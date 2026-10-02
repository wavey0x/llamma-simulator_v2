"""Run from a staged source directory, never against the authoritative checkout."""
from setuptools import Extension, setup
from Cython.Build import cythonize
import json
import os
from pathlib import Path

lock = json.loads(Path("build-config.json").read_text())
sdk = Path(os.environ["SDKROOT"])
setup(ext_modules=cythonize(
    [Extension(name, [name.replace(".", "/") + ".py"], language=lock["language"],
               extra_compile_args=lock["flags"] + ["-ffile-prefix-map=.=yrisk-cython", "-isysroot", str(sdk),
                                                  "-isystem", str(sdk / "usr/include/c++/v1")],
               extra_link_args=lock.get("link_flags", []))
     for name in lock["modules"]],
    compiler_directives=lock["directives"],
    annotate=True,
))
