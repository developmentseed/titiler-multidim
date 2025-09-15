ARG PYTHON_VERSION=3.12

FROM --platform=linux/amd64 public.ecr.aws/lambda/python:${PYTHON_VERSION}
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /tmp

# Install system dependencies to compile (numexpr)
RUN dnf install -y gcc-c++

COPY uv.lock .python-version pyproject.toml LICENSE README.md ./
COPY titiler/ ./titiler/

RUN uv export --locked --no-editable --no-dev --extra lambda --format requirements.txt -o requirements.txt && \
  uv pip install --compile-bytecode --no-binary pydantic --target /asset -r requirements.txt 


# Reduce package size and remove useless files
RUN cd /asset && find . -type f -name '*.pyc' | while read f; do n=$(echo $f | sed 's/__pycache__\///' | sed 's/.cpython-[0-9]*//'); cp $f $n; done;
RUN cd /asset && find . -type d -a -name '__pycache__' -print0 | xargs -0 rm -rf
RUN cd /asset && find . -type f -a -name '*.py' -print0 | xargs -0 rm -f
RUN find /asset -type d -a -name 'tests' -print0 | xargs -0 rm -rf
RUN rm -rdf /asset/numpy/doc/ /asset/bin /asset/geos_license /asset/Misc
RUN rm -rdf /asset/boto3*
RUN rm -rdf /asset/botocore*

# Strip debug symbols from compiled C/C++ code (except for numpy.libs!)
RUN cd /asset && \
  find . -type f -name '*.so*' \
  -not -path "./numpy.libs/*" \
  -exec strip --strip-unneeded {} \;

COPY infrastructure/aws/lambda/handler.py /asset/handler.py

CMD ["echo", "hello world"]
