import os
import pytest
import yaml
from collections import namedtuple
from unittest import mock

import numpy as np
import pandas as pd
import sklearn.datasets as datasets
import lightgbm as lgb

import mlflow.lightgbm
import mlflow.utils
import mlflow.pyfunc.scoring_server as pyfunc_scoring_server
from mlflow import pyfunc
from mlflow.models.utils import _read_example
from mlflow.models import Model, infer_signature
from mlflow.store.artifact.s3_artifact_repo import S3ArtifactRepository
from mlflow.tracking.artifact_utils import _download_artifact_from_uri
from mlflow.utils.environment import _mlflow_conda_env
from mlflow.utils.file_utils import TempDir
from mlflow.utils.model_utils import _get_flavor_configuration
from mlflow.tracking._model_registry import DEFAULT_AWAIT_MAX_SLEEP_SECONDS

from tests.helper_functions import set_boto_credentials  # pylint: disable=unused-import
from tests.helper_functions import mock_s3_bucket  # pylint: disable=unused-import
from tests.helper_functions import (
    pyfunc_serve_and_score_model,
    _compare_conda_env_requirements,
    _assert_pip_requirements,
    _is_available_on_pypi,
)

EXTRA_PYFUNC_SERVING_TEST_ARGS = [] if _is_available_on_pypi("lightgbm") else ["--no-conda"]

ModelWithData = namedtuple("ModelWithData", ["model", "inference_dataframe"])


@pytest.fixture(scope="session")
def lgb_model():
    iris = datasets.load_iris()
    X = pd.DataFrame(
        iris.data[:, :2], columns=iris.feature_names[:2]  # we only take the first two features.
    )
    y = iris.target

    dtrain = lgb.Dataset(X, y)
    model = lgb.train({"objective": "multiclass", "num_class": 3}, dtrain)
    return ModelWithData(model=model, inference_dataframe=X)


@pytest.fixture
def model_path(tmpdir):
    return os.path.join(str(tmpdir), "model")


@pytest.fixture
def lgb_custom_env(tmpdir):
    conda_env = os.path.join(str(tmpdir), "conda_env.yml")
    _mlflow_conda_env(conda_env, additional_pip_deps=["lightgbm", "pytest"])
    return conda_env


@pytest.mark.large
def test_model_save_load(lgb_model, model_path):
    model = lgb_model.model

    mlflow.lightgbm.save_model(lgb_model=model, path=model_path)
    reloaded_model = mlflow.lightgbm.load_model(model_uri=model_path)
    reloaded_pyfunc = pyfunc.load_pyfunc(model_uri=model_path)

    np.testing.assert_array_almost_equal(
        model.predict(lgb_model.inference_dataframe),
        reloaded_model.predict(lgb_model.inference_dataframe),
    )

    np.testing.assert_array_almost_equal(
        reloaded_model.predict(lgb_model.inference_dataframe),
        reloaded_pyfunc.predict(lgb_model.inference_dataframe),
    )


def test_signature_and_examples_are_saved_correctly(lgb_model):
    model = lgb_model.model
    X = lgb_model.inference_dataframe
    signature_ = infer_signature(X)
    example_ = X.head(3)
    for signature in (None, signature_):
        for example in (None, example_):
            with TempDir() as tmp:
                path = tmp.path("model")
                mlflow.lightgbm.save_model(
                    model, path=path, signature=signature, input_example=example
                )
                mlflow_model = Model.load(path)
                assert signature == mlflow_model.signature
                if example is None:
                    assert mlflow_model.saved_input_example_info is None
                else:
                    assert all((_read_example(mlflow_model, path) == example).all())


@pytest.mark.large
def test_model_load_from_remote_uri_succeeds(lgb_model, model_path, mock_s3_bucket):
    mlflow.lightgbm.save_model(lgb_model=lgb_model.model, path=model_path)

    artifact_root = "s3://{bucket_name}".format(bucket_name=mock_s3_bucket)
    artifact_path = "model"
    artifact_repo = S3ArtifactRepository(artifact_root)
    artifact_repo.log_artifacts(model_path, artifact_path=artifact_path)

    model_uri = artifact_root + "/" + artifact_path
    reloaded_model = mlflow.lightgbm.load_model(model_uri=model_uri)
    np.testing.assert_array_almost_equal(
        lgb_model.model.predict(lgb_model.inference_dataframe),
        reloaded_model.predict(lgb_model.inference_dataframe),
    )


@pytest.mark.large
def test_model_log(lgb_model, model_path):
    old_uri = mlflow.get_tracking_uri()
    model = lgb_model.model
    with TempDir(chdr=True, remove_on_exit=True) as tmp:
        for should_start_run in [False, True]:
            try:
                mlflow.set_tracking_uri("test")
                if should_start_run:
                    mlflow.start_run()

                artifact_path = "model"
                conda_env = os.path.join(tmp.path(), "conda_env.yaml")
                _mlflow_conda_env(conda_env, additional_pip_deps=["xgboost"])

                mlflow.lightgbm.log_model(
                    lgb_model=model, artifact_path=artifact_path, conda_env=conda_env
                )
                model_uri = "runs:/{run_id}/{artifact_path}".format(
                    run_id=mlflow.active_run().info.run_id, artifact_path=artifact_path
                )
                reloaded_model = mlflow.lightgbm.load_model(model_uri=model_uri)
                np.testing.assert_array_almost_equal(
                    model.predict(lgb_model.inference_dataframe),
                    reloaded_model.predict(lgb_model.inference_dataframe),
                )

                model_path = _download_artifact_from_uri(artifact_uri=model_uri)
                model_config = Model.load(os.path.join(model_path, "MLmodel"))
                assert pyfunc.FLAVOR_NAME in model_config.flavors
                assert pyfunc.ENV in model_config.flavors[pyfunc.FLAVOR_NAME]
                env_path = model_config.flavors[pyfunc.FLAVOR_NAME][pyfunc.ENV]
                assert os.path.exists(os.path.join(model_path, env_path))

            finally:
                mlflow.end_run()
                mlflow.set_tracking_uri(old_uri)


def test_log_model_calls_register_model(lgb_model):
    artifact_path = "model"
    register_model_patch = mock.patch("mlflow.register_model")
    with mlflow.start_run(), register_model_patch, TempDir(chdr=True, remove_on_exit=True) as tmp:
        conda_env = os.path.join(tmp.path(), "conda_env.yaml")
        _mlflow_conda_env(conda_env, additional_pip_deps=["lightgbm"])
        mlflow.lightgbm.log_model(
            lgb_model=lgb_model.model,
            artifact_path=artifact_path,
            conda_env=conda_env,
            registered_model_name="AdsModel1",
        )
        model_uri = "runs:/{run_id}/{artifact_path}".format(
            run_id=mlflow.active_run().info.run_id, artifact_path=artifact_path
        )
        mlflow.register_model.assert_called_once_with(
            model_uri, "AdsModel1", await_registration_for=DEFAULT_AWAIT_MAX_SLEEP_SECONDS
        )


def test_log_model_no_registered_model_name(lgb_model):
    artifact_path = "model"
    register_model_patch = mock.patch("mlflow.register_model")
    with mlflow.start_run(), register_model_patch, TempDir(chdr=True, remove_on_exit=True) as tmp:
        conda_env = os.path.join(tmp.path(), "conda_env.yaml")
        _mlflow_conda_env(conda_env, additional_pip_deps=["lightgbm"])
        mlflow.lightgbm.log_model(
            lgb_model=lgb_model.model, artifact_path=artifact_path, conda_env=conda_env
        )
        mlflow.register_model.assert_not_called()


@pytest.mark.large
def test_model_save_persists_specified_conda_env_in_mlflow_model_directory(
    lgb_model, model_path, lgb_custom_env
):
    mlflow.lightgbm.save_model(lgb_model=lgb_model.model, path=model_path, conda_env=lgb_custom_env)

    pyfunc_conf = _get_flavor_configuration(model_path=model_path, flavor_name=pyfunc.FLAVOR_NAME)
    saved_conda_env_path = os.path.join(model_path, pyfunc_conf[pyfunc.ENV])
    assert os.path.exists(saved_conda_env_path)
    assert saved_conda_env_path != lgb_custom_env

    with open(lgb_custom_env, "r") as f:
        lgb_custom_env_parsed = yaml.safe_load(f)
    with open(saved_conda_env_path, "r") as f:
        saved_conda_env_parsed = yaml.safe_load(f)
    assert saved_conda_env_parsed == lgb_custom_env_parsed


@pytest.mark.large
def test_model_save_persists_requirements_in_mlflow_model_directory(
    lgb_model, model_path, lgb_custom_env
):
    mlflow.lightgbm.save_model(lgb_model=lgb_model.model, path=model_path, conda_env=lgb_custom_env)

    saved_pip_req_path = os.path.join(model_path, "requirements.txt")
    _compare_conda_env_requirements(lgb_custom_env, saved_pip_req_path)


@pytest.mark.large
def test_log_model_with_pip_requirements(lgb_model, tmpdir):
    # Path to a requirements file
    req_file = tmpdir.join("requirements.txt")
    req_file.write("a")
    with mlflow.start_run():
        mlflow.lightgbm.log_model(lgb_model.model, "model", pip_requirements=req_file.strpath)
        _assert_pip_requirements(mlflow.get_artifact_uri("model"), ["mlflow", "a"])

    # List of requirements
    with mlflow.start_run():
        mlflow.lightgbm.log_model(
            lgb_model.model, "model", pip_requirements=[f"-r {req_file.strpath}", "b"]
        )
        _assert_pip_requirements(mlflow.get_artifact_uri("model"), ["mlflow", "a", "b"])

    # Constraints file
    with mlflow.start_run():
        mlflow.lightgbm.log_model(
            lgb_model.model, "model", pip_requirements=[f"-c {req_file.strpath}", "b"]
        )
        _assert_pip_requirements(
            mlflow.get_artifact_uri("model"), ["mlflow", "b", "-c constraints.txt"], ["a"]
        )


@pytest.mark.large
def test_log_model_with_extra_pip_requirements(lgb_model, tmpdir):
    default_reqs = mlflow.lightgbm.get_default_pip_requirements()

    # Path to a requirements file
    req_file = tmpdir.join("requirements.txt")
    req_file.write("a")
    with mlflow.start_run():
        mlflow.lightgbm.log_model(lgb_model.model, "model", extra_pip_requirements=req_file.strpath)
        _assert_pip_requirements(mlflow.get_artifact_uri("model"), ["mlflow", *default_reqs, "a"])

    # List of requirements
    with mlflow.start_run():
        mlflow.lightgbm.log_model(
            lgb_model.model, "model", extra_pip_requirements=[f"-r {req_file.strpath}", "b"]
        )
        _assert_pip_requirements(
            mlflow.get_artifact_uri("model"), ["mlflow", *default_reqs, "a", "b"]
        )

    # Constraints file
    with mlflow.start_run():
        mlflow.lightgbm.log_model(
            lgb_model.model, "model", extra_pip_requirements=[f"-c {req_file.strpath}", "b"]
        )
        _assert_pip_requirements(
            mlflow.get_artifact_uri("model"),
            ["mlflow", *default_reqs, "b", "-c constraints.txt"],
            ["a"],
        )


@pytest.mark.large
def test_model_save_accepts_conda_env_as_dict(lgb_model, model_path):
    conda_env = dict(mlflow.lightgbm.get_default_conda_env())
    conda_env["dependencies"].append("pytest")
    mlflow.lightgbm.save_model(lgb_model=lgb_model.model, path=model_path, conda_env=conda_env)

    pyfunc_conf = _get_flavor_configuration(model_path=model_path, flavor_name=pyfunc.FLAVOR_NAME)
    saved_conda_env_path = os.path.join(model_path, pyfunc_conf[pyfunc.ENV])
    assert os.path.exists(saved_conda_env_path)

    with open(saved_conda_env_path, "r") as f:
        saved_conda_env_parsed = yaml.safe_load(f)
    assert saved_conda_env_parsed == conda_env


@pytest.mark.large
def test_model_log_persists_specified_conda_env_in_mlflow_model_directory(
    lgb_model, lgb_custom_env
):
    artifact_path = "model"
    with mlflow.start_run():
        mlflow.lightgbm.log_model(
            lgb_model=lgb_model.model, artifact_path=artifact_path, conda_env=lgb_custom_env
        )
        model_uri = "runs:/{run_id}/{artifact_path}".format(
            run_id=mlflow.active_run().info.run_id, artifact_path=artifact_path
        )

    model_path = _download_artifact_from_uri(artifact_uri=model_uri)
    pyfunc_conf = _get_flavor_configuration(model_path=model_path, flavor_name=pyfunc.FLAVOR_NAME)
    saved_conda_env_path = os.path.join(model_path, pyfunc_conf[pyfunc.ENV])
    assert os.path.exists(saved_conda_env_path)
    assert saved_conda_env_path != lgb_custom_env

    with open(lgb_custom_env, "r") as f:
        lgb_custom_env_parsed = yaml.safe_load(f)
    with open(saved_conda_env_path, "r") as f:
        saved_conda_env_parsed = yaml.safe_load(f)
    assert saved_conda_env_parsed == lgb_custom_env_parsed


@pytest.mark.large
def test_model_log_persists_requirements_in_mlflow_model_directory(lgb_model, lgb_custom_env):
    artifact_path = "model"
    with mlflow.start_run():
        mlflow.lightgbm.log_model(
            lgb_model=lgb_model.model, artifact_path=artifact_path, conda_env=lgb_custom_env
        )
        model_uri = "runs:/{run_id}/{artifact_path}".format(
            run_id=mlflow.active_run().info.run_id, artifact_path=artifact_path
        )

    model_path = _download_artifact_from_uri(artifact_uri=model_uri)
    saved_pip_req_path = os.path.join(model_path, "requirements.txt")
    _compare_conda_env_requirements(lgb_custom_env, saved_pip_req_path)


@pytest.mark.large
def test_model_save_without_specified_conda_env_uses_default_env_with_expected_dependencies(
    lgb_model, model_path
):
    mlflow.lightgbm.save_model(lgb_model=lgb_model.model, path=model_path, conda_env=None)

    pyfunc_conf = _get_flavor_configuration(model_path=model_path, flavor_name=pyfunc.FLAVOR_NAME)
    conda_env_path = os.path.join(model_path, pyfunc_conf[pyfunc.ENV])
    with open(conda_env_path, "r") as f:
        conda_env = yaml.safe_load(f)

    assert conda_env == mlflow.lightgbm.get_default_conda_env()


@pytest.mark.large
def test_model_log_without_specified_conda_env_uses_default_env_with_expected_dependencies(
    lgb_model,
):
    artifact_path = "model"
    with mlflow.start_run():
        mlflow.lightgbm.log_model(
            lgb_model=lgb_model.model, artifact_path=artifact_path, conda_env=None
        )
        model_uri = "runs:/{run_id}/{artifact_path}".format(
            run_id=mlflow.active_run().info.run_id, artifact_path=artifact_path
        )

    model_path = _download_artifact_from_uri(artifact_uri=model_uri)
    pyfunc_conf = _get_flavor_configuration(model_path=model_path, flavor_name=pyfunc.FLAVOR_NAME)
    conda_env_path = os.path.join(model_path, pyfunc_conf[pyfunc.ENV])
    with open(conda_env_path, "r") as f:
        conda_env = yaml.safe_load(f)

    assert conda_env == mlflow.lightgbm.get_default_conda_env()


@pytest.mark.large
def test_pyfunc_serve_and_score(lgb_model):
    model, inference_dataframe = lgb_model
    artifact_path = "model"
    with mlflow.start_run():
        mlflow.lightgbm.log_model(model, artifact_path)
        model_uri = mlflow.get_artifact_uri(artifact_path)

    resp = pyfunc_serve_and_score_model(
        model_uri,
        data=inference_dataframe,
        content_type=pyfunc_scoring_server.CONTENT_TYPE_JSON_SPLIT_ORIENTED,
        extra_args=EXTRA_PYFUNC_SERVING_TEST_ARGS,
    )
    scores = pd.read_json(resp.content, orient="records").values.squeeze()
    np.testing.assert_array_almost_equal(scores, model.predict(inference_dataframe))
