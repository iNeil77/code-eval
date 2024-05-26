# Better use newer Python as generated code can use new features
FROM python:3.10-slim

# install git, g++ and python3-tk
RUN apt-get update && apt-get install -y git g++ python3-tk zip unzip procps r-base

# upgrade to latest pip
RUN pip install --upgrade pip

# Add a new user "wildcodeuser"
RUN adduser --disabled-password --gecos "" wildcodeuser

# Acquire benchmark code to local
RUN git clone -b neil-exps https://github.com/iNeil77/code-eval.git /wildcode

RUN cd /wildcode && pip install . && pip install -U -I -r https://raw.githubusercontent.com/bigcode-project/wildcodebench-annotation/main/requirements.txt

# Pre-install the dataset
RUN python3 -c "from wildcode.data import get_wildcodebench; get_wildcodebench()"

WORKDIR /wildcode

ENTRYPOINT ["python3", "-m", "wildcode.evaluate"]