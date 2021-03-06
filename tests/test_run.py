"""
End-to-end tests for running directly in the operating system.
"""

import json
from pprint import pformat
from unittest import TestCase, skipIf, skipUnless
from urllib.request import urlopen
from subprocess import (
    check_output,
    Popen,
    PIPE,
    check_call,
    run,
    STDOUT,
)
import time
import os

from .utils import (
    DIRECTORY,
    random_name,
    run_webserver,
    telepresence_version,
    current_namespace,
    OPENSHIFT,
    KUBECTL,
    query_in_k8s,
)

REGISTRY = os.environ.get("TELEPRESENCE_REGISTRY", "datawire")
# inject-tcp/vpn-tcp/container:
TELEPRESENCE_METHOD = os.environ.get("TELEPRESENCE_METHOD", None)
# If this env variable is set, we know we're using minikube or minishift:
LOCAL_VM = os.environ.get("TELEPRESENCE_LOCAL_VM") is not None

EXISTING_DEPLOYMENT = """\
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {name}
  namespace: {namespace}
data:
  EXAMPLE_ENVFROM: foobar
  EX_MULTI_LINE: |
    first line (no newline before, newline after)
    second line (newline before and after)
---
%s
metadata:
  name: {name}
  namespace: {namespace}
spec:
  replicas: {replicas}
  template:
    metadata:
      labels:
        name: {name}
        hello: monkeys  # <-- used by volumes test
    spec:
      containers:
      # Extra container at start to demonstrate we can handle multiple
      # containers
      - name: getintheway
        image: openshift/hello-openshift
        resources:
          limits:
            cpu: "100m"
            memory: "150Mi"
      - name: {container_name}
        image: {image}
        envFrom:
        - configMapRef:
            name: {name}
        env:
        - name: MYENV
          value: hello
        volumeMounts:
        - name: podinfo
          mountPath: /podinfo
        resources:
          requests:
            cpu: "100m"
            memory: "150Mi"
          limits:
            cpu: "100m"
            memory: "150Mi"
      volumes:
      - name: podinfo
        downwardAPI:
          items:
            - path: "labels"
              fieldRef:
                fieldPath: metadata.labels
"""

if OPENSHIFT:
    EXISTING_DEPLOYMENT = EXISTING_DEPLOYMENT % ("""\
apiVersion: v1
kind: DeploymentConfig""",)
    DEPLOYMENT_TYPE = "deploymentconfig"
else:
    EXISTING_DEPLOYMENT = EXISTING_DEPLOYMENT % ("""\
apiVersion: extensions/v1beta1
kind: Deployment""",)
    DEPLOYMENT_TYPE = "deployment"

NAMESPACE_YAML = """\
apiVersion: v1
kind: Namespace
metadata:
  name: {}
"""


def run_script_test(telepresence_args, local_command):
    """Run a script with Telepresence."""
    p = Popen(
        args=["telepresence"] + telepresence_args + [
            "--logfile",
            "-",
            "--method",
            TELEPRESENCE_METHOD,
            "--run-shell",
        ],
        cwd=str(DIRECTORY),
        stdin=PIPE,
    )
    p.stdin.write(bytes(local_command, "ascii") + b"\n")
    p.stdin.flush()
    p.stdin.close()
    return p.wait()


def assert_fromcluster(namespace, service_name, port, telepresence_process):
    """Assert that there's a webserver accessible from the cluster."""
    url = "http://{}:{}/__init__.py".format(service_name, port)
    expected = (DIRECTORY / "__init__.py").read_bytes()
    for i in range(30):
        result = query_in_k8s(namespace, url, telepresence_process)
        if result != expected:
            time.sleep(1)
        else:
            break
    assert result == expected
    print("Hooray, got expected result when querying via cluster.")


@skipIf(TELEPRESENCE_METHOD == "container", "non-Docker tests")
class NativeEndToEndTests(TestCase):
    """
    End-to-end tests on the native machine.
    """

    @skipIf(OPENSHIFT, "OpenShift Online doesn't do namespaces")
    def create_namespace(self):
        """Create a new namespace, return its name."""
        name = random_name()
        yaml = NAMESPACE_YAML.format(name).encode("utf-8")
        check_output(
            args=[
                KUBECTL,
                "apply",
                "-f",
                "-",
            ],
            input=yaml,
        )
        self.addCleanup(
            lambda: check_output([KUBECTL, "delete", "namespace", name])
        )
        return name

    # TODO test default namespace behavior
    def fromcluster(
        self, telepresence_args, url, namespace, local_port, remote_port=None
    ):
        """
        Test of communication from the cluster.

        Start webserver that serves files from this directory. Run HTTP query
        against it on the Kubernetes cluster, compare to real file.
        """
        if remote_port is None:
            port_string = str(local_port)
            remote_port = local_port
        else:
            port_string = "{}:{}".format(local_port, remote_port)

        args = ["telepresence"] + telepresence_args + [
            "--expose",
            port_string,
            "--logfile",
            "-",
            "--method",
            TELEPRESENCE_METHOD,
            "--run-shell",
        ]
        p = Popen(args=args, stdin=PIPE, stderr=STDOUT, cwd=str(DIRECTORY))
        p.stdin.write(
            ("sleep 1; exec python3 -m http.server %s\n" %
             (local_port, )).encode("ascii")
        )
        p.stdin.flush()
        try:
            assert_fromcluster(namespace, url, remote_port, p)
        finally:
            p.stdin.close()
            p.terminate()
            p.wait()

    def test_fromcluster(self):
        """
        Communicate from the cluster to Telepresence, with default namespace.
        """
        service_name = random_name()
        self.fromcluster(
            ["--new-deployment", service_name],
            service_name,
            current_namespace(),
            12370,
        )

    def test_fromcluster_custom_local_port(self):
        """
        The cluster can talk to a process running in a Docker container, with
        the local process listening on a different port.
        """
        service_name = random_name()
        remote_port = 12360
        local_port = 12355
        p = Popen(
            args=[
                "telepresence", "--new-deployment", service_name, "--expose",
                "{}:{}".format(local_port, remote_port), "--logfile", "-",
                "--method", TELEPRESENCE_METHOD, "--run", "python3", "-m",
                "http.server", str(local_port)
            ],
            cwd=str(DIRECTORY),
        )
        assert_fromcluster(current_namespace(), service_name, remote_port, p)
        p.terminate()
        p.wait()

    def test_fromcluster_with_namespace(self):
        """
        Communicate from the cluster to Telepresence, with custom namespace.
        """
        namespace = self.create_namespace()
        service_name = random_name()
        self.fromcluster(
            ["--new-deployment", service_name, "--namespace", namespace],
            "{}.{}.svc.cluster.local".format(service_name, namespace),
            namespace,
            12347,
        )

    @skipIf(OPENSHIFT, "OpenShift never allows running containers as root.")
    def test_fromcluster_port_lt_1024(self):
        """
        Communicate from the cluster to Telepresence, with port<1024.
        """
        service_name = random_name()
        self.fromcluster(
            ["--new-deployment", service_name],
            service_name,
            current_namespace(),
            12399,
            70,
        )

    @skipIf(OPENSHIFT, "OpenShift never allows running containers as root.")
    def test_swapdeployment_fromcluster_port_lt_1024(self):
        """
        Communicate from the cluster to Telepresence, with port<1024, using
        swap-deployment because omg it's a different code path. Yay.
        """
        # Create a non-Telepresence deployment:
        service_name = random_name()
        check_call([
            KUBECTL,
            "run",
            service_name,
            "--port=79",
            "--expose",
            "--restart=Always",
            "--image=openshift/hello-openshift",
            "--replicas=2",
            "--labels=telepresence-test=" + service_name,
            "--env=HELLO=there",
        ])
        self.addCleanup(
            check_call, [KUBECTL, "delete", DEPLOYMENT_TYPE, service_name]
        )
        self.fromcluster(
            ["--swap-deployment", service_name],
            service_name,
            current_namespace(),
            12398,
            79,
        )

    def test_disconnect(self):
        """Telepresence exits if the connection is lost."""
        exit_code = run_script_test(["--new-deployment", random_name()],
                                    "python3 disconnect.py")
        # Exit code 3 means proxy exited prematurely:
        assert exit_code == 3

    @skipIf(
        LOCAL_VM and TELEPRESENCE_METHOD == "vpn-tcp",
        "--deployment doesn't work on local VMs with vpn-tcp method."
    )
    def existingdeployment(self, namespace, script):
        if namespace is None:
            namespace = current_namespace()
        webserver_name = run_webserver(namespace)

        # Create a Deployment outside of Telepresence:
        name = random_name()
        image = "{}/telepresence-k8s:{}".format(
            REGISTRY, telepresence_version()
        )
        deployment = EXISTING_DEPLOYMENT.format(
            name=name,
            container_name=name,
            image=image,
            namespace=namespace,
            replicas="1",
        )
        check_output(
            args=[
                KUBECTL,
                "apply",
                "-f",
                "-",
            ],
            input=deployment.encode("utf-8")
        )

        def cleanup():
            check_output([
                KUBECTL, "delete", DEPLOYMENT_TYPE, name,
                "--namespace=" + namespace
            ])
            check_output([
                KUBECTL, "delete", "ConfigMap", name,
                "--namespace=" + namespace
            ])
        self.addCleanup(cleanup)

        args = ["--deployment", name, "--namespace", namespace]
        exit_code = run_script_test(
            args, "python3 {} {} {}".format(
                script,
                webserver_name,
                namespace,
            )
        )
        assert 113 == exit_code

    # XXX Test existing deployment w/ default namespace

    def test_swapdeployment(self):
        """
        --swap-deployment swaps out Telepresence pod and then swaps it back on
        exit, when original pod was created with `kubectl run` or `oc run`.
        """
        # Create a non-Telepresence deployment:
        name = random_name()
        check_call([
            KUBECTL,
            "run",
            name,
            "--restart=Always",
            "--image=openshift/hello-openshift",
            "--replicas=2",
            "--labels=telepresence-test=" + name,
            "--env=HELLO=there",
        ])
        self.addCleanup(check_call, [KUBECTL, "delete", DEPLOYMENT_TYPE, name])
        self.assert_swapdeployment(name, 2, "telepresence-test=" + name)

    def test_swapdeployment_swap_args(self):
        """
        --swap-deployment swaps out Telepresence pod and overrides the entrypoint.
        """
        # Create a non-Telepresence deployment:
        name = random_name()
        check_call([
            KUBECTL,
            "run",
            name,
            "--restart=Always",
            "--image=openshift/hello-openshift",
            "--replicas=2",
            "--labels=telepresence-test=" + name,
            "--env=HELLO=there",
            "--",
            "/hello-openshift",
        ])
        self.addCleanup(check_call, [KUBECTL, "delete", DEPLOYMENT_TYPE, name])
        self.assert_swapdeployment(name, 2, "telepresence-test=" + name)

    @skipIf(not OPENSHIFT, "Only runs on OpenShift")
    def test_swapdeployment_ocnewapp(self):
        """
        --swap-deployment works on pods created via `oc new-app`.
        """
        name = random_name()
        check_call([
            "oc",
            "new-app",
            "--name=" + name,
            "--docker-image=openshift/hello-openshift",
            "--env=HELLO=there",
        ])
        self.addCleanup(
            check_call, ["oc", "delete", "dc,imagestream,service", name]
        )
        self.assert_swapdeployment(name, 1, "app=" + name)

    def assert_swapdeployment(self, name, replicas, selector):
        """
        --swap-deployment swaps out Telepresence pod and then swaps it back on
        exit.
        """
        webserver_name = run_webserver()
        p = Popen(
            args=[
                "telepresence", "--swap-deployment", name, "--logfile", "-",
                "--method", TELEPRESENCE_METHOD, "--run", "python3",
                "tocluster.py", webserver_name, current_namespace(),
                "HELLO=there"
            ],
            cwd=str(DIRECTORY),
        )
        exit_code = p.wait()
        assert 113 == exit_code
        deployment = json.loads(
            str(
                check_output([
                    KUBECTL, "get", DEPLOYMENT_TYPE, name, "-o", "json",
                    "--export"
                ]), "utf-8"
            )
        )
        # We swapped back:
        assert deployment["spec"]["replicas"] == replicas

        # Ensure pods swap back too:
        start = time.time()
        while True:
            pods = json.loads(
                str(
                    check_output([
                        KUBECTL, "get", "pod", "--selector=" + selector, "-o",
                        "json", "--export"
                    ]), "utf-8"
                )
            )["items"]
            image_and_phase = list(
                (pod["spec"]["containers"][0]["image"],
                 pod["status"]["phase"])
                for pod
                in pods
            )
            if all(
                    image.startswith("openshift/hello-openshift")
                    for (image, phase)
                    in image_and_phase
            ):
                print("Found openshift!")
                return
            time.sleep(1)

            if time.time() - start > 60:
                assert False, \
                    "Didn't switch back to openshift: \n\t{}\n{}".format(
                        image_and_phase,
                        pformat(json.loads(check_output([
                            KUBECTL, "get", "-o", "json", "all",
                            "--selector", selector,
                        ]))),
                    )

    def test_swapdeployment_auto_expose(self):
        """
        --swap-deployment auto-exposes ports listed in the Deployment.

        Important that the test uses port actually used by original container,
        otherwise we will miss bugs where a telepresence proxy container is
        added rather than being swapped.
        """
        service_name = random_name()
        check_call([
            KUBECTL,
            "run",
            service_name,
            "--port=8888",
            "--expose",
            "--restart=Always",
            "--image=openshift/hello-openshift",
            "--replicas=2",
            "--labels=telepresence-test=" + service_name,
            "--env=HELLO=there",
        ])
        self.addCleanup(
            check_call, [KUBECTL, "delete", DEPLOYMENT_TYPE, service_name]
        )
        port = 8888
        # Explicitly do NOT use '--expose 8888', to see if it's auto-detected:
        p = Popen(
            args=[
                "telepresence", "--swap-deployment", service_name,
                "--logfile", "-", "--method", TELEPRESENCE_METHOD,
                "--run", "python3", "-m",
                "http.server", str(port)
            ],
            cwd=str(DIRECTORY),
        )

        assert_fromcluster(current_namespace(), service_name, port, p)
        p.terminate()
        p.wait()


@skipUnless(TELEPRESENCE_METHOD == "container", "requires Docker")
class DockerEndToEndTests(TestCase):
    """End-to-end tests on Docker."""

    def get_containers(self):
        return set(check_output(["docker", "ps", "-q"]).split())

    def setUp(self):
        self.containers = self.get_containers()

    def tearDown(self):
        # Ensure no container leaks
        time.sleep(1)
        assert self.containers == self.get_containers()

    def test_fromcluster(self):
        """
        The cluster can talk to a process running in a Docker container.
        """
        service_name = random_name()
        port = 12350
        p = Popen(
            args=[
                "telepresence", "--new-deployment", service_name, "--expose",
                str(port), "--logfile", "-", "--method", "container",
                "--docker-run", "-v", "{}:/host".format(DIRECTORY),
                "--workdir", "/host", "python:3-alpine", "python3", "-m",
                "http.server", str(port)
            ],
        )

        assert_fromcluster(current_namespace(), service_name, port, p)
        p.terminate()
        p.wait()

    def test_fromcluster_custom_local_port(self):
        """
        The cluster can talk to a process running in a Docker container, with
        the local process listening on a different port.
        """
        service_name = random_name()
        remote_port = 12350
        local_port = 7777
        p = Popen(
            args=[
                "telepresence", "--new-deployment",
                service_name, "--expose", "{}:{}".format(
                    local_port, remote_port
                ), "--logfile", "-", "--method", "container", "--docker-run",
                "-v", "{}:/host".format(DIRECTORY), "--workdir", "/host",
                "python:3-alpine", "python3", "-m", "http.server",
                str(local_port)
            ],
        )
        try:
            assert_fromcluster(
                current_namespace(), service_name, remote_port, p
            )
        finally:
            p.terminate()
            p.wait()
