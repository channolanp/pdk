import os
import time

import torch
from determined.experimental import Determined
from determined.pytorch import load_trial_from_checkpoint_path

from kserve import KServeClient
from common import (
    upload_model,
    get_version,
    DeterminedInfo,
    KServeInfo,
    ModelInfo,
    check_existence,
    create_inference_service,
    wait_for_deployment,
    parse_args,
)

# =====================================================================================


def create_scriptmodule(det_master, det_user, det_pw, model_name, pach_id):
    print(
        f"Loading model version '{model_name}/{pach_id}' from master at '{det_master}...'"
    )

    if os.environ["HOME"] == "/":
        os.environ["HOME"] = "/app"

    os.environ["SERVING_MODE"] = "true"

    start = time.time()
    client = Determined(master=det_master, user=det_user, password=det_pw)
    version = get_version(client, model_name, pach_id)
    checkpoint = version.checkpoint
    checkpoint_dir = checkpoint.download()
    trial = load_trial_from_checkpoint_path(
        checkpoint_dir, map_location=torch.device("cpu")
    )
    end = time.time()
    delta = end - start
    print(f"Checkpoint loaded in {delta} seconds.")

    print(f"Creating ScriptModule from Determined checkpoint...")

    # Create ScriptModule
    m = torch.jit.script(trial.model)

    # Save ScriptModule to file
    torch.jit.save(m, "scriptmodule.pt")
    print(f"ScriptModule created successfully.")


# =====================================================================================


def create_mar_file(model_name, model_version):
    print(f"Creating .mar file for model '{model_name}'...")
    os.system(
        "torch-model-archiver --model-name %s --version %s --serialized-file ./scriptmodule.pt --handler ./customer_churn_handler.py --extra-files \"./numscale.json\" --force"
        % (model_name, model_version)
    )
    print(f"Created .mar file successfully.")


# =====================================================================================


def create_properties_file(model_name, model_version, cloud_model_host):
    print(f"--> Cloud Model Host: {cloud_model_host}")
    model_store = "/mnt/models/model-store"
    if cloud_model_host == "aws":
        print("--> Changing Model Store to match AWS")
        model_store = "/mnt/models"        
    config_properties = """inference_address=http://0.0.0.0:8085
management_address=http://0.0.0.0:8083
metrics_address=http://0.0.0.0:8082
grpc_inference_port=7070
grpc_management_port=7071
enable_envvars_config=true
install_py_dep_per_model=true
enable_metrics_api=true
metrics_format=prometheus
NUM_WORKERS=1
number_of_netty_threads=4
job_queue_size=10
model_store=%s
model_snapshot={"name":"startup.cfg","modelCount":1,"models":{"%s":{"%s":{"defaultVersion":true,"marName":"%s.mar","minWorkers":1,"maxWorkers":5,"batchSize":1,"maxBatchDelay":5000,"responseTimeout":120}}}}""" % (
        model_store,
        model_name,
        model_version,
        model_name,
    )

    conf_prop = open("config.properties", "w")
    n = conf_prop.write(config_properties)
    conf_prop.close()

    model_files = ["config.properties", str(model_name) + ".mar"]

    return model_files


# =====================================================================================


def main():
    args = parse_args()
    det = DeterminedInfo()
    ksrv = KServeInfo()
    model = ModelInfo("/pfs/data/model-info.yaml")

    if args.google_application_credentials:
        os.environ[
            "GOOGLE_APPLICATION_CREDENTIALS"
        ] = args.google_application_credentials

    print(
        f"Starting pipeline: deploy-name='{args.deployment_name}', model='{model.name}', version='{model.version}'"
    )

    # Pull Determined.AI Checkpoint, load it, and create ScriptModule (TorchScript)
    create_scriptmodule(
        det.master, det.username, det.password, model.name, model.version
    )

    # Create .mar file from ScriptModule
    create_mar_file(model.name, model.version)

    # Create config.properties for .mar file, return files to upload to GCS bucket
    model_files = create_properties_file(model.name, model.version, args.cloud_model_host)

    # Upload model artifacts to Cloud  bucket in the format for TorchServe
    upload_model(
        model.name, model_files, args.cloud_model_host, args.cloud_model_bucket
    )

    # Instantiate KServe Client using kubeconfig
    if args.k8s_config_file:
        print(f"Using Configured K8s Config File at {args.k8s_config_file}")
        kclient = KServeClient(config_file=args.k8s_config_file)
    else:
        kclient = KServeClient()

    # Check if a previous version of the InferenceService exists (return true/false)
    replace = check_existence(kclient, args.deployment_name, ksrv.namespace)

    resource_requirements = {"requests": {}, "limits": {}}
    if args.resource_requests:
        resource_requirements["requests"] = dict(
            [i.split("=") for i in args.resource_requests]
        )
    if args.resource_limits:
        resource_requirements["limits"] = dict(
            [i.split("=") for i in args.resource_limits]
        )
    # Create or replace inference service
    create_inference_service(
        kclient,
        ksrv.namespace,
        model.name,
        args.deployment_name,
        model.version,
        replace,
        args.cloud_model_host,
        args.cloud_model_bucket,
        args.tolerations,
        resource_requirements,
        args.service_account_name,
        "v1",
    )
    if args.wait and args.cloud_model_host:
        # Wait for InferenceService to be ready for predictions
        wait_for_deployment(
            kclient, ksrv.namespace, args.deployment_name, model.name
        )

    print(
        f"Ending pipeline: deploy-name='{args.deployment_name}', model='{model.name}', version='{model.version}'"
    )


# =====================================================================================


if __name__ == "__main__":
    main()
