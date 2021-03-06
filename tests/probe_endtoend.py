"""
This probe runs in a Telepresence created and managed execution context.
It observes things about that environment and reports about them on stdout.
The report can be inspected by the test suite to verify Telepresence has
created the execution context correctly.
"""

from os import (
    environ,
)
from sys import (
    stdin,
    stdout,
)
from struct import (
    pack,
)
from os.path import (
    join,
)
from json import (
    dumps,
    loads,
)
from argparse import (
    ArgumentParser,
)
from urllib.request import (
    Request,
    urlopen,
)
from subprocess import (
    CalledProcessError,
    check_output,
)

# The probe's output is mixed together with Telepresence output and maybe more
# output from things like socat or torsocks.  This makes it difficult to
# extract information from the probe via stdout.  Unfortunately, options apart
# from stdout as a channel for communicating back to the test process are
# limit.  Depending on the Telepresence method, it may not be easy for the
# test process to reach the probe via normal networking means (we may not have
# an address we can bind to that the host can reach; there may not be an
# address on the host we can reach).  It's not possible to pass an
# already-opened connection from the test process to the probe process because
# it would need to pass through Telepresence and Telepresence doesn't support
# this.  Even a UNIX socket on the host filesystem isn't necessarily reachable
# from the probe process.
#
# So use the channel we have: stdout.  To deal with the fact that there is
# other output on it, frame structured probe output with a magic prefix
# followed by a length prefix followed by the content itself.  The test
# process will have to filter through the noise to find the magic prefix and
# then use the length prefix to know how much following data is relevant.
#
# The particular prefix chosen is not legal UTF-8.  Hopefully this minimizes
# the chances that some legitimate output will accidentally duplicate it.
MAGIC_PREFIX = b"\xc0\xc1\xfe\xff"

def main():
    parser = argument_parser()
    args = parser.parse_args()

    output = TaggedOutput(stdout.buffer)

    result = dumps({
        "environ": dict(environ),
        "probe-urls": list(probe_urls(args.probe_url)),
        "probe-commands": list(probe_commands(args.probe_command)),
        "probe-paths": list(probe_paths(args.probe_path)),
    })

    output.write(result)
    read_and_respond(stdin.buffer, output)
    print("Goodbye.")


class TaggedOutput(object):
    def __init__(self, stdout):
        self.stdout = stdout


    def write(self, text):
        data = text.encode("utf-8")
        self.stdout.write(MAGIC_PREFIX + pack(">I", len(data)) + data)
        self.stdout.flush()



def read_and_respond(commands, output):
    while True:
        line = commands.readline().decode("utf-8")
        print("Read line: {!r}".format(line), flush=True)
        if not line:
            print("Closed? {}".format(commands.closed), flush=True)
            from time import sleep
            sleep(1)
            continue
        argv = line.split()
        command = argv.pop(0)
        print("Read command: {}".format(command), flush=True)
        response = COMMANDS[command](*argv)
        output.write(dumps(response))
        print("Dumped response.", flush=True)



def probe_also_proxy(hostname):
    # We must use http to avoid SNI problems.
    url = "http://{}/ip".format(hostname)
    # And we must specify the host header to avoid vhost problems.
    request = Request(url, None, {"Host": "httpbin.org"})

    print("Retrieving {}".format(url))
    try:
        response = str(urlopen(request, timeout=30).read(), "utf-8")
    except Exception as e:
        print("Got error: {}".format(e))
        result = (False, str(e))
    else:
        print("Got {} from webserver.".format(repr(response)))
        request_ip = loads(response)["origin"]
        result = (True, request_ip)
    return result


COMMANDS = {
    "probe-url": lambda *urls: list(probe_urls(urls)),
    "probe-also-proxy": probe_also_proxy,
}



def probe_urls(urls):
    for url in urls:
        print("Retrieving {}".format(url))
        try:
            response = urlopen(url, timeout=30).read()
        except Exception as e:
            print("Got error: {}".format(e))
            result = (False, str(e))
        else:
            print("Got {} bytes".format(len(response)))
            result = (True, response.decode("utf-8"))
        yield (url, result)


def probe_commands(commands):
    for command in commands:
        try:
            output = check_output([command, "arg1"])
        except CalledProcessError as e:
            result = (False, e.returncode)
        except FileNotFoundError:
            result = (False, None)
        else:
            result = (True, output.decode("utf-8"))
        yield (command, result)


def probe_paths(paths):
    root = environ["TELEPRESENCE_ROOT"]
    for path in paths:
        try:
            with open(join(root, path)) as f:
                yield (path, f.read())
        except FileNotFoundError:
            yield (path, None)


def argument_parser():
    parser = ArgumentParser()
    parser.add_argument(
        "--probe-url",
        action="append",
        help="A URL to retrieve.",
    )
    parser.add_argument(
        "--probe-command",
        action="append",
        help="A command to run.",
    )
    parser.add_argument(
        "--probe-path",
        action="append",
        help="A path to read.",
    )
    return parser


main()
