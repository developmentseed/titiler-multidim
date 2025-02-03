# titiler-multidim

---

**Source Code**: <a href="https://github.com/developmentseed/titiler-multidim" target="_blank">https://github.com/developmentseed/titiler-multidim</a>

---

## Running Locally

```bash
# It's recommended to install dependencies in a virtual environment
python -m venv .venv
source .venv/bin/activate

python -m pip install -e . uvicorn
export TEST_ENVIRONMENT=true  # set this when running locally to mock redis
uvicorn titiler.multidim.main:app --reload
```

To access the docs, visit <http://127.0.0.1:8000/api.html>.
![](https://github.com/developmentseed/titiler-multidim/assets/10407788/4368546b-5b60-4cd5-86be-fdd959374b17)

## Development

Tests use data generated locally by using `tests/fixtures/generate_test_*.py` scripts.

Install the package using [`uv`](https://docs.astral.sh/uv/getting-started/installation/) with all development dependencies:

```bash
uv sync
uv run pre-commit install
```

To run all the tests:

```bash
uv run pytest
```

To run just one test:

```bash
uv run pytest tests/test_app.py::test_get_info 
```

## VEDA Deployment

The Github Actions workflow defined in [.github/workflows/ci.yml](./.github/workflows/ci.yml) deploys code to AWS for the VEDA project.

* There are 2 stacks - one production and one development.
* The production stack is deployed when the `main` branch is tagged, creating a new release. The production stack will deploy to a stack with an API Gateway associated with the domain prod-titiler-xarray.delta-backend.com/.
* The development stack will be deployed upon pushes to the `dev` and `main` branches. The development stack will deploy to a stack with an API Gateway associated with the domain dev-titiler-xarray.delta-backend.com/.

## New Deployments

The following steps detail how to to setup and deploy the CDK stack from your local machine.

1. Install CDK and connect to your AWS account. This step is only necessary once per AWS account.

    ```bash
    # Download titiler repo
    git clone https://github.com/developmentseed/titiler-xarray.git

    # Install with the deployment dependencies
    uv sync --group deployment

    # Install node dependency
    uv run npm --prefix infrastructure/aws install

    # Deploys the CDK toolkit stack into an AWS environment
    uv run npm --prefix infrastructure/aws run cdk -- bootstrap

    # or to a specific region and or using AWS profile
    AWS_DEFAULT_REGION=us-west-2 AWS_REGION=us-west-2 AWS_PROFILE=myprofile npm --prefix infrastructure/aws run cdk -- bootstrap
    ```

2. Update settings

    Set environment variable or hard code in `infrastructure/aws/.env` file (e.g `STACK_STAGE=testing`).

3. Pre-Generate CFN template

    ```bash
    uv run npm --prefix infrastructure/aws run cdk -- synth  # Synthesizes and prints the CloudFormation template for this stack
    ```

4. Deploy

    ```bash
    STACK_STAGE=staging uv run npm --prefix infrastructure/aws run cdk -- deploy titiler-xarray-staging

    # Deploy in specific region
    AWS_DEFAULT_REGION=us-west-2 AWS_REGION=us-west-2 AWS_PROFILE=smce-veda STACK_STAGE=production  uv run npm --prefix infrastructure/aws run cdk -- deploy titiler-xarray-production
    ```

**Important**

In AWS Lambda environment we need to have specific version of botocore, S3FS, FSPEC and other libraries.
To make sure the application will both work locally and in AWS Lambda environment you can install the dependencies using `python -m pip install -r infrastructure/aws/requirement-lambda.txt`
