import sys
import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from time import time
import json
from datetime import datetime

from omegaconf import OmegaConf, DictConfig

from cluster_experiment_utils.cluster_utils.base_cluster_utils import BaseClusterUtils
from cluster_experiment_utils.utils import run_cmd_check_output, printed_sleep, run_cmd


@dataclass
class Args:
    my_job_id: str = None
    conf: str = None  # Path
    varying_param_key: str = None


def parse_args():
    parser = argparse.ArgumentParser(description="Submit Dask Job.")

    parser.add_argument(
        "--my-job-id", metavar="i", required=True, help="job id generated by our script"
    )

    parser.add_argument("--conf", help="Yaml configuration file", required=True)

    parser.add_argument("--varying_param_key", required=True)

    if len(sys.argv) == 1:
        parser.print_help()

    ags = parser.parse_args()
    return Args(
        my_job_id=ags.my_job_id, conf=ags.conf, varying_param_key=ags.varying_param_key
    )


def start_scheduler(preload_scheduler_cmd, rep_dir, scheduler_file, gpu_type):
    if gpu_type == "amd":
        visible_device = "export ROCR_VISIBLE_DEVICES=''"
    elif gpu_type == "nvidia":
        visible_device = "export CUDA_VISIBLE_DEVICES=''"
    else:
        raise ValueError("Unknown gpu")

    scheduler_cmd = f"{visible_device} && dask scheduler {preload_scheduler_cmd}  --no-dashboard --no-show --scheduler-file {scheduler_file}"  
    # TODO: check about this # --interface='ib0', not sure if we have this on Frontier.

    print("Starting Scheduler")
    cluster_utils = BaseClusterUtils.get_instance()
    cluster_utils.run_job(
        cmd=scheduler_cmd,
        node_count=1,
        process_count=1,
        processes_per_node=1,
        gpus_per_job=0,
        stderr=f"{rep_dir}/scheduler.err",
        stdout=f"{rep_dir}/scheduler.out",
    )
    total_wait_time = 160  # in seconds
    print(
        f"Waiting up to {total_wait_time} seconds so the scheduler file can be created."
    )
    wait_time = 3
    start_waiting_time = time()
    elapsed_time = 0
    while not os.path.exists(scheduler_file) and elapsed_time < total_wait_time:
        printed_sleep(wait_time)
        elapsed_time = time() - start_waiting_time
        print(f"Still waiting for {scheduler_file}")
    if os.path.exists(scheduler_file):
        print(f"File '{scheduler_file}' found after {elapsed_time} seconds")
        print("Scheduler started!")
        return True
    else:
        print(f"File '{scheduler_file}' not found after {total_wait_time} seconds")
        return False


def start_workers_with_gpu(nnodes, n_gpus_per_node, gpu_type, rep_dir, scheduler_file, dask_workers_startup_wait_in_sec):
    # From: https://docs.olcf.ornl.gov/systems/frontier_user_guide.html
    # Due to the unique architecture of Frontier compute nodes and the way
    # that Slurm currently allocates GPUs and CPU cores to job steps,
    # it is suggested that all 8 GPUs on a node are allocated to the job step
    # to ensure that optimal bindings are possible.

    if gpu_type == "amd":
        visible_device = "ROCR_VISIBLE_DEVICES"
    elif gpu_type == "nvidia":
        visible_device = "CUDA_VISIBLE_DEVICES"
    else:
        raise ValueError("Unknown gpu")

    worker_logs = os.path.join(rep_dir, "worker_logs")
    os.makedirs(worker_logs, exist_ok=True)
    cluster_utils = BaseClusterUtils.get_instance()

    for i in range(nnodes):
        worker_cmds = []
        for j in range(n_gpus_per_node):
            stdout = os.path.join(worker_logs, f"worker_{i}_{j}.out")
            stderr = os.path.join(worker_logs, f"worker_{i}_{j}.err")
            #  --interface ib0
            worker_cmd = f"export {visible_device}={j} && dask worker --nthreads 1 --nworkers 1 --no-dashboard  --scheduler-file {scheduler_file} > {stdout} 2> {stderr} "
            worker_cmds.append(worker_cmd)
        worker_cmds_str = " && ".join(worker_cmds)
        cluster_utils.run_job(worker_cmds_str, node_count=1, processes_per_node=n_gpus_per_node, gpus_per_job=n_gpus_per_node)

    print(
        f"\n\nDone starting {nnodes*n_gpus_per_node} workers. Let's just wait some time...\n\n"
    )
    printed_sleep(dask_workers_startup_wait_in_sec)


def start_client(conf_data, varying_param_key, rep_dir, scheduler_file, with_flowcept_arg):
    print("Starting the Client")
    python_client_command = "python " + conf_data.static_params.dask_user_workflow

    wf_params = OmegaConf.to_container(conf_data["varying_params"][varying_param_key].get("workflow_params", {}))
    print(wf_params)
    
    client_cmd_args = {
        "rep-dir": rep_dir,
        "scheduler-file": scheduler_file,        
    }
    if len(wf_params):
        client_cmd_args["workflow-params"] = f"'{json.dumps(wf_params)}'"
    
    for k, v in client_cmd_args.items():
        python_client_command = python_client_command.replace("$[" + k + "_val]", v)
    
    python_client_command += " " + with_flowcept_arg

    print("Command after replacements")
    print(python_client_command)
    
    t_c_i = time()
    run_cmd_check_output(python_client_command)

    t_c_f = time()
    printed_sleep(15)
    return t_c_f, t_c_i


def start_flowcept(exp_conf, job_hosts, rep_dir, varying_param_key):
    print("Importing flowcept_utils")
    from cluster_experiment_utils.flowcept_utils import (
        update_flowcept_settings,
        kill_dbs,
        start_redis,
        start_mongo
    )
    print("Done importing.")

    cluster_utils = BaseClusterUtils.get_instance()
    should_start_mongo = exp_conf.static_params.start_mongo
    flowcept_base_settings_path = exp_conf["static_params"][
        "flowcept_base_settings_path"
    ]
    dask_scheduler_setup_path = exp_conf["static_params"]["dask_scheduler_setup_path"]
    preload_scheduler_cmd = f"--preload {dask_scheduler_setup_path}"
    flowcept_settings = OmegaConf.load(Path(flowcept_base_settings_path))

    db_host = list(job_hosts.keys())[0]
    print(f"DB Host: {db_host}")
    job_id = cluster_utils.get_this_job_id()
    update_flowcept_settings(
        exp_conf,
        flowcept_settings,
        db_host,
        should_start_mongo,
        rep_dir,
        varying_param_key,
        job_id,
    )
    flowcept_settings_path = os.path.join(rep_dir, "flowcept_settings.yaml")
    os.environ["FLOWCEPT_SETTINGS_PATH"] = flowcept_settings_path
    kill_dbs(db_host, should_start_mongo)
    printed_sleep(6)
    redis_start_command = exp_conf.static_params.redis_start_command
    start_redis(db_host, redis_start_command)
    if should_start_mongo:
        mongo_start_cmd = exp_conf.static_params.mongo_start_command
        start_mongo(db_host, mongo_start_cmd, rep_dir)

    from flowcept import FlowceptConsumerAPI

    consumer = FlowceptConsumerAPI()
    consumer.start()
    return consumer, flowcept_settings, preload_scheduler_cmd


def main(
    exp_conf: DictConfig,
    varying_param_key: str,
    my_job_id,
    rep_no: int,
):
    cluster_utils = BaseClusterUtils.get_instance()
    print("Killing old job steps")
    cluster_utils.kill_all_running_job_steps()
    
    proj_dir = exp_conf.static_params.proj_dir
    job_dir = os.path.join(proj_dir, "exps", my_job_id)
    rep_dir = os.path.join(job_dir, str(rep_no))
    os.makedirs(rep_dir, exist_ok=True)
    nnodes = exp_conf.varying_params[varying_param_key].get("nnodes")
    n_gpus_per_node = exp_conf.static_params.get("n_gpus_per_node")
    dask_workers_startup_wait_in_sec = exp_conf.static_params.dask_workers_startup_wait_in_sec
    gpu_type = exp_conf.static_params.get("gpu_type")
    scheduler_file = os.path.join(rep_dir, "scheduler_info.json")

    with_flowcept = exp_conf.varying_params[varying_param_key].get(
        "with_flowcept", False
    )
    with_flowcept_arg = "--with-flowcept" if with_flowcept else ""

    python_env = run_cmd_check_output("which python")
    print(f"Using python: {python_env}") 

    job_hosts = cluster_utils.get_job_hosts()    
    preload_scheduler_cmd = ""

    t0 = time()    
    printed_sleep(2)

    consumer = None
    flowcept_settings = None
    if with_flowcept:
        consumer, flowcept_settings, preload_scheduler_cmd = start_flowcept(
            exp_conf, job_hosts, rep_dir, varying_param_key
        )

    if not start_scheduler(preload_scheduler_cmd, rep_dir, scheduler_file, gpu_type):
        return -1

    printed_sleep(3)
    start_workers_with_gpu(nnodes, n_gpus_per_node, gpu_type, rep_dir, scheduler_file, dask_workers_startup_wait_in_sec)

    t_c_f, t_c_i = start_client(exp_conf, varying_param_key, rep_dir, scheduler_file, with_flowcept_arg)
    print("Workflow done!")
    if consumer is not None:
        print("Now going to gracefully stop everything")
        consumer.stop()

    workflow_result_file = os.path.join(rep_dir, "workflow_result.json")
    
    if os.path.exists(workflow_result_file):        
        with open(json_file_path, 'r') as json_file:
            workflow_result = json.load(json_file)
    else:        
        workflow_result = None

    t1 = time()
    job_output = cluster_utils.generate_job_output(
        exp_conf,
        job_hosts,
        job_dir,
        my_job_id,
        proj_dir,
        python_env,
        rep_dir,
        rep_no,
        t0,
        t1,
        t_c_f,
        t_c_i,
        varying_param_key,
        workflow_result,
        with_flowcept,
        flowcept_settings
    )

    if with_flowcept:
        from cluster_experiment_utils.flowcept_utils import test_data_and_persist
        test_data_and_persist(rep_dir, wf_result, job_output)

    print("All done. Going to kill all runnnig job steps.")
    cluster_utils.kill_all_running_job_steps()
    


if __name__ == "__main__":
    args = parse_args()

    exp_conf = OmegaConf.load(Path(args.conf))

    nreps = exp_conf.varying_params[args.varying_param_key]["nreps"]
    for rep_no in range(nreps):
        main(
            exp_conf=exp_conf,
            varying_param_key=args.varying_param_key,
            my_job_id=args.my_job_id,
            rep_no=rep_no,
        )

    proj_dir = exp_conf.static_params.proj_dir
    job_dir = os.path.join(proj_dir, "exps", args.my_job_id)
    os.makedirs(job_dir, exist_ok=True)
    with open(os.path.join(job_dir, "SUCCESS"), "w") as f:
        f.write(datetime.utcnow().strftime("%Y-%m-%d %H-%M-%S.%f")[:-3])

    # TODO: uncomment
    #BaseClusterUtils.get_instance().kill_this_job()
    sys.exit(0)
