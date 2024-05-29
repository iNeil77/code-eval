import argparse
import json
import multiprocessing
import os
import pickle
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Tuple
from warnings import warn

import numpy as np
from termcolor import cprint
from tqdm import tqdm

from wildcode.data import (
    get_wildcodebench,
    get_wildcodebench_hash,
    load_solutions,
)
from wildcode.data.utils import CACHE_DIR
from wildcode.eval import (
    PASS,
    compatible_eval_result,
    estimate_pass_at_k,
    untrusted_check,
)
from wildcode.gen.util import trusted_exec

# 1st item: the status
# 2nd item (optional): the detailed pass/fail boolean for each input
Result = Tuple[str, List[bool]]


def get_groundtruth(problems, hashcode, check_gt_only):
    cache_file = os.path.join(CACHE_DIR, f"{hashcode}.pkl")
    if os.path.exists(cache_file):
        if check_gt_only:
            os.remove(cache_file)
        else:
            print(f"Load from ground-truth from {cache_file}")
            with open(cache_file, "rb") as f:
                return pickle.load(f)

    os.makedirs(CACHE_DIR, exist_ok=True)
    print("\nAsserting the groundtruth...")
    tbegin = time.time()
    expected_time = {}
    for task_id, problem in tqdm(problems.items()):
        expected_time[task_id] = trusted_exec(
            problem["prompt"] + "\n" + problem["clean_canonical_solution"],
            problem["test"],
            problem["task_id"],
        )
    print(f"Expected outputs computed in {time.time() - tbegin:.2f}s")
    
    with open(cache_file, "wb") as f:
        pickle.dump(expected_time, f)

    return expected_time

def check_correctness(
    dataset: str,
    completion_id: int,
    problem: Dict[str, Any],
    solution: str,
    identifier=None,
    min_time_limit: float = 0.1,
    gt_time_limit: float = 2.0
) -> Dict[str, Result]:  # {...}, "base" | "plus" -> (status, details)
    ret = {
        "completion_id": completion_id,
        "task_id": problem["task_id"],
        "_identifier": identifier,
        "solution": solution,
    }
    ret["base"] = untrusted_check(
        dataset,
        solution,
        problem["test"],
        problem["entry_point"],
        min_time_limit,
        gt_time_limit
    )
    return ret


def evaluate(flags):
    if flags.parallel is None:
        n_workers = max(1, multiprocessing.cpu_count() // 2)
    else:
        n_workers = flags.parallel

    if flags.check_gt_only:
        # bypass the samples
        flags.samples = "__dummy__.jsonl"
        
    assert flags.samples.endswith(".jsonl")
    if flags.base_dir is None:
        summary_path = flags.samples.replace(".jsonl", "_summary.json")
        if flags.reprompt:
            result_path = flags.samples.replace(".jsonl", "_reprompt_eval_results.json")
        else:
            result_path = flags.samples.replace(".jsonl", "_eval_results.json")
    else:
        summary_path = os.path.join(flags.base_dir, "summary.json")
        result_path = os.path.join(flags.base_dir, "eval_results.json")

    if os.path.isfile(result_path):
        print(f"Load from previous results from {result_path}")
        with open(result_path, "r") as f:
            results = json.load(f)

        results = compatible_eval_result(results)
    else:
        if flags.dataset == "wildcodebench":
            problems = get_wildcodebench()
            dataset_hash = get_wildcodebench_hash()
            if not flags.check_solution_only:
                expected_time = get_groundtruth(problems, dataset_hash, flags.check_gt_only)
        
        if flags.check_gt_only:
            return
        
        results = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "eval": {},
        }

        with ProcessPoolExecutor(max_workers=n_workers) as executor:
            futures = []
            completion_id = Counter()
            n_samples = 0
            eval_results = defaultdict(list)  # task_id ->
            remainings = set()

            print("Reading samples...")
            for sample in tqdm(load_solutions(flags.samples)):
                task_id = sample["task_id"]
                
                if task_id not in problems:
                    warn(
                        f"Task {task_id} is found in the samples but not found in the dataset"
                    )
                    continue
                solution = (
                    sample["solution"]
                    if "solution" in sample
                    else problems[task_id]["prompt"] + sample["completion"]
                )
                if flags.reprompt:
                    solution = problems[task_id]["prompt_wo_doc"] + "\n    pass\n" + solution
                remainings.add(sample["_identifier"])
                args = (
                    flags.dataset,
                    completion_id[task_id],
                    problems[task_id],
                    solution,
                    sample["_identifier"],
                    flags.min_time_limit,
                    expected_time[task_id] if not flags.check_solution_only else flags.min_time_limit
                )
                futures.append(executor.submit(check_correctness, *args))
                completion_id[task_id] += 1
                n_samples += 1

            assert n_samples == len(remainings), "Missing problems in unfinished"
            assert len(completion_id) == len(problems), "Missing problems in samples"

            def stucking_checker():
                while remainings:
                    last_size = len(remainings)
                    time.sleep(120)
                    if last_size != len(remainings) or len(remainings) == 0:
                        continue
                    # Potential stucking
                    warn("No samples had finished testing in the last 120s")
                    warn(f"{len(remainings)} samples to be tested: {remainings}")

            threading.Thread(target=stucking_checker).start()

            for future in tqdm(as_completed(futures), total=n_samples):
                result = future.result()
                remainings.remove(result["_identifier"])
                eval_results[result["task_id"]].append(result)

        # sort the results for each problem by completion_id
        for task_id, task_results in eval_results.items():
            task_results.sort(key=lambda x: x["completion_id"])
            results["eval"][task_id] = []
            for res in task_results:
                stat, details = res["base"]
                results["eval"][task_id].append(
                    {
                        "task_id": task_id,
                        "solution": res["solution"],
                        "status": stat,
                        "details": details,
                    }
                )

    # Calculate pass@k.
    total = np.array([len(r) for r in results["eval"].values()])
    base_correct = []

    for res in results["eval"].values():
        bc = sum([r["status"] == PASS for r in res])
        base_correct.append(bc)

    base_correct = np.array(base_correct)

    pass_at_k = {
        f"pass@{k}": estimate_pass_at_k(total, base_correct, k).mean()
        for k in [1, 5, 10, 25, 100]
        if total.min() >= k
    }
    cprint(f"{flags.dataset}", "green")
    for k, v in pass_at_k.items():
        cprint(f"{k}:\t{v:.3f}", "green")

    # save results
    if os.path.isfile(result_path):
        try:
            # mv the file to a backup
            new_path = result_path + ".bak"
            while os.path.isfile(new_path):
                new_path += ".bak"
            os.rename(result_path, new_path)
            print(f"Backup {result_path} to {new_path}")
            os.makedirs(os.path.dirname(result_path), exist_ok=True)
            os.chmod(os.path.dirname(result_path), 0o777)
            with open(result_path, "w") as f:
                json.dump(results, f)
            with open(summary_path, "w") as f:
                json.dump(pass_at_k, f)
        except Exception as e:
            warn(f"Failed to save the results due to Exceptions: {e}")
            return
    else:
        try:
            os.makedirs(os.path.dirname(result_path), exist_ok=True)
            os.chmod(os.path.dirname(result_path), 0o777)
            with open(result_path, "w") as f:
                json.dump(results, f)
            with open(summary_path, "w") as f:
                json.dump(pass_at_k, f)
        except Exception as e:
            warn(f"Failed to save the results due to Exceptions: {e}")
            return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", required=True, type=str, choices=["wildcodebench"]
    )
    parser.add_argument("--samples", required=True, type=str)
    parser.add_argument("--parallel", default=None, type=int)
    parser.add_argument("--min-time-limit", default=1, type=float)
    parser.add_argument("--base-dir", default=None, type=str)
    parser.add_argument("--reprompt", action="store_true", help="Prepend the prompt again")
    parser.add_argument("--check-gt-only", action="store_true", help="Check the groundtruth")
    parser.add_argument("--check-solution-only", action="store_true", help="Check the solution only")
    args = parser.parse_args()

    evaluate(args)


if __name__ == "__main__":
    main()
