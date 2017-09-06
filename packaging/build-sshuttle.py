#!/usr/bin/env python3
"""
Create a standalone sshuttle.

We use a particular commit off of upstream master since at the moment there is no release with the feature we want (as of July 18, 2017). Once a new release is made we can pin that.

For now we have a fork with a branch; hope is to upstream our changes
eventually.
"""

import os
from subprocess import check_call, check_output
from tempfile import mkdtemp


def main():
    tempdir = mkdtemp() + "/sshuttle"
    check_call([
        "git", "clone", "https://github.com/datawire/sshuttle.git", tempdir
    ])
    check_call(["git", "checkout", "llmnr"],
               cwd=tempdir)
    check_call(["python3", "setup.py", "sdist"], cwd=tempdir)
    version = str(
        check_output(["python3", "setup.py", "--version"],
                     cwd=tempdir).strip(), "ascii"
    )
    dest = os.path.join(
        os.path.abspath(os.getcwd()), "virtualenv", "bin",
        "sshuttle-telepresence"
    )
    print(dest)
    check_call([
        os.path.abspath(os.getcwd()), "virtualenv", "bin", "dist/sshuttle-{}.tar.gz".format(version), "-o", dest,
        "--python-shebang=/usr/bin/env python3", "-c", "sshuttle"
    ],
               cwd=tempdir, env=os.environ.copy())


if __name__ == '__main__':
    main()
