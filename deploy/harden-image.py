"""Runs once while building the image. Removes the shell, its tools, and the package manager.

It's written in Python because the shell can't reliably delete itself and the tools it would need.
"""
import glob
import os
import shutil

for folder in ("/bin", "/sbin", "/usr/bin", "/usr/sbin"):
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.islink(path) and "busybox" in os.readlink(path):
            os.remove(path)
for path in ("/bin/sh", "/bin/ash", "/bin/busybox", "/sbin/apk", __file__):
    if os.path.lexists(path):
        os.remove(path)
# pip and its helpers, wherever the base image put them. The app's own packages are in /opt/packages.
leftovers = [item for name in ("pip", "setuptools", "wheel") for item in glob.glob(f"/usr/local/lib/python*/site-packages/{name}*")]
for path in ("/etc/apk", "/usr/share/apk", "/var/cache/apk", *leftovers):
    shutil.rmtree(path, ignore_errors=True)
