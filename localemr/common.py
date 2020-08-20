"""
TODO: This is a shittily named module to solve circular dependencies. Make it better.
"""
import os
import re
from typing import Optional, List
from datetime import datetime
from distutils.version import StrictVersion
from xml.sax.saxutils import escape
import pytz
from moto.emr.models import FakeStep
from moto.emr.exceptions import EmrError


class ClusterSubset:
    def __init__(self, state, name=None, release_label=None, start_datetime=None, ready_datetime=None,
                 end_datetime=None):
        self.name = name
        self.release_label = release_label
        self.state = state
        self.start_datetime = start_datetime
        self.ready_datetime = ready_datetime
        self.end_datetime = end_datetime

    def run_bootstrap_actions(self):
        self.ready_datetime = datetime.now(pytz.utc)
        self.state = EmrClusterState.WAITING

    def run_termination_actions(self):
        self.end_datetime = datetime.now(pytz.utc)
        self.state = EmrClusterState.TERMINATED


AWS_SCRIPT_RUNNERS = {'command-runner.jar', 'script-runner.jar'}
UNWANTED_CONFIGS = ['--master', '--num-executors', '--driver-memory', '--executor-memory', '--deploy-mode']


class LocalFakeStep(FakeStep):
    def __init__(
            self,
            hostname: str,
            cluster_id: str,
            cluster_name: str,
            main_class: Optional[str],
            **kwargs):
        super().__init__(**kwargs)
        self.failure_details = FailureDetails()
        self.hostname = hostname
        self.cluster_id = cluster_id
        self.cluster_name = cluster_name
        self.main_class = main_class

    def start(self):
        self.start_datetime = datetime.now(pytz.utc)

    def to_cli_args(self) -> List[str]:
        if self.jar in AWS_SCRIPT_RUNNERS:
            return self.clean_for_local_run([self.extract_basename_if_not_exists(self.args[0])] + self.args[1:])

        return self.clean_for_local_run(['hadoop', 'jar', self.jar, self.main_class] + self.args)

    @staticmethod
    def extract_basename_if_not_exists(command: str) -> str:
        return command if os.path.exists(command) or os.path.islink(command) else os.path.basename(command)

    @staticmethod
    def filter_unwanted_config(args: List[str], unwanted_configs=None):
        unwanted_configs = unwanted_configs or UNWANTED_CONFIGS
        for conf in unwanted_configs:
            if conf in args:
                i = args.index(conf)
                args = LocalFakeStep.remove_beside(args, i)
        return args

    @staticmethod
    def remove_beside(ls: list, i: int):
        return ls if i < 0 else ls[:i] + ls[i + 2:]

    @staticmethod
    def convert_s3_to_s3a_path(emr_step: List[str]) -> List[str]:
        return [re.sub(r's3://|s3n://', 's3a://', v) for v in emr_step]

    @staticmethod
    def clean_for_local_run(emr_step: List[str]) -> List[str]:
        return LocalFakeStep.convert_s3_to_s3a_path(LocalFakeStep.filter_unwanted_config(emr_step))


class FailureDetails:
    def __init__(self, reason=None, message=None, log_file=None):
        self.reason = escape(reason) if reason else None
        self.message = escape(message) if message else None
        self.log_file = escape(log_file) if log_file else None

    def to_dict(self):
        return {
            'Reason': self.reason,
            'Message': self.message,
            'LogFile': self.log_file,
        }


class EmrStepState:
    PENDING = 'PENDING'
    CANCEL_PENDING = 'CANCEL_PENDING'
    RUNNING = 'RUNNING'
    COMPLETED = 'COMPLETED'
    CANCELLED = 'CANCELLED'
    FAILED = 'FAILED'
    INTERRUPTED = 'INTERRUPTED'


EMR_STEP_TERMINAL_STATES = [
    EmrStepState.FAILED,
    EmrStepState.CANCELLED,
    EmrStepState.INTERRUPTED
]


class SparkResult:
    def __init__(self, state: EmrStepState, failure_details: FailureDetails):
        self.state = state
        self.failure_details = failure_details


class ActionOnFailure:
    TERMINATE_JOB_FLOW = 'TERMINATE_JOB_FLOW'
    TERMINATE_CLUSTER = 'TERMINATE_CLUSTER'
    CANCEL_AND_WAIT = 'CANCEL_AND_WAIT'
    CONTINUE = 'CONTINUE'


class EmrClusterState:
    STARTING = 'STARTING'
    WAITING = 'WAITING'
    BOOTSTRAPPING = 'BOOTSTRAPPING'
    RUNNING = 'RUNNING'
    TERMINATING = 'TERMINATING'
    TERMINATED = 'TERMINATED'
    TERMINATED_WITH_ERRORS = 'TERMINATED_WITH_ERRORS'


EMR_CLUSTER_TERMINAL_STATES = [
    EmrClusterState.TERMINATED,
    EmrClusterState.TERMINATED_WITH_ERRORS
]

# There must be a docker image on davlum/localemr-container
# with a matching Spark version for this to work.
EMR_TO_APPLICATION_VERSION = {
    '5.0.0': {'Spark': '2.0.0'},
    '5.0.3': {'Spark': '2.0.1'},
    '5.2.0': {'Spark': '2.0.2'},
    '5.3.0': {'Spark': '2.1.0'},
    '5.6.0': {'Spark': '2.1.1'},
    '5.8.0': {'Spark': '2.2.0'},
    '5.11.0': {'Spark': '2.2.1'},
    '5.13.0': {'Spark': '2.3.0'},
    '5.16.0': {'Spark': '2.3.1'},
    '5.18.0': {'Spark': '2.3.2'},
    '5.20.0': {'Spark': '2.4.0'},
    '5.24.0': {'Spark': '2.4.2'},
    '5.25.0': {'Spark': '2.4.3'},
    '5.27.0': {'Spark': '2.4.4'},
    '6.0.0': {'Spark': '2.4.5'},
}


def parse_release_label(cluster_release_label):
    try:
        return re.findall(r'emr-(\d+\.\d+\.\d+)', cluster_release_label)[0]
    except IndexError:
        aws_docs = 'https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-release-components.html'
        message = "{} is not a valid emr release label. See {} for more info".format(
            cluster_release_label, aws_docs
        )
        raise EmrError(
            error_type="ValidationException",
            message=message,
            template="error_json",

        )


def get_emr_version(cluster_release_label):
    """
    Parameters
    ----------
    cluster_release_label : a string of form 'emr-{semver}'

    Returns
    -------
    The corresponding EMR version

    Assumes the EMR versions from EMR_VERSION_TO_APPLICATION_VERSION are sorted smallest to largest
    """
    emr_version = parse_release_label(cluster_release_label)
    parsed_emr_version = StrictVersion(emr_version)
    versions = list(EMR_TO_APPLICATION_VERSION.keys())
    last_version = versions[0]
    if parsed_emr_version <= StrictVersion(last_version):
        return last_version
    for current_version in versions[1:]:
        parsed_current_version = StrictVersion(current_version)
        if parsed_emr_version == parsed_current_version:
            return emr_version
        if StrictVersion(last_version) < parsed_emr_version < parsed_current_version:
            return last_version
        last_version = current_version

    return versions[-1]


def cluster_to_spark_version(cluster: ClusterSubset) -> dict:
    return EMR_TO_APPLICATION_VERSION[get_emr_version(cluster.release_label)]['Spark']
