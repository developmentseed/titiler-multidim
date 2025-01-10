ARG PYTHON_VERSION=3.10

FROM --platform=linux/amd64 public.ecr.aws/lambda/python:${PYTHON_VERSION}
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /tmp

# Install system dependencies to compile (numexpr)
RUN yum install -y gcc-c++

COPY pyproject.toml pyproject.toml
COPY LICENSE LICENSE
COPY README.md README.md
COPY src/titiler/ src/titiler/

# Install dependencies
# HACK: aiobotocore has a tight botocore dependency
# https://github.com/aio-libs/aiobotocore/issues/862
# and becaise we NEED to remove both boto3 and botocore to save space for the package
# we have to force using old package version that seems `almost` compatible with Lambda env botocore
# https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html
RUN uv pip install --system .   "mangum>=0.10.0" "botocore==1.29.76" "aiobotocore==2.5.0" --target /asset --no-binary pydantic

# Reduce package size and remove useless files
RUN cd /asset && find . -type f -name '*.pyc' | while read f; do n=$(echo $f | sed 's/__pycache__\///' | sed 's/.cpython-[0-9]*//'); cp $f $n; done;
RUN cd /asset && find . -type d -a -name '__pycache__' -print0 | xargs -0 rm -rf
RUN cd /asset && find . -type f -a -name '*.py' -print0 | xargs -0 rm -f
RUN find /asset -type d -a -name 'tests' -print0 | xargs -0 rm -rf
RUN rm -rdf /asset/numpy/doc/ /asset/bin /asset/geos_license /asset/Misc
RUN rm -rdf /asset/boto3*
RUN rm -rdf /asset/botocore*

COPY infrastructure/aws/lambda/handler.py /asset/handler.py

CMD ["echo", "hello world"]
