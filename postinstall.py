import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

import git
import torch
from packaging import version as pv

from importlib import metadata

from packaging.version import Version

from extensions.sd_dreambooth_extension.dreambooth import shared as db_shared

torch_index_url = os.environ.get('TORCH_INDEX_URL', "https://download.pytorch.org/whl/cu121")
torch_command = os.environ.get('TORCH_COMMAND', f"pip install torch==2.1.2 torchvision==0.16.2 --extra-index-url {torch_index_url}")
# Detect CUDA version
try:
    import torch
    cuda_major, cuda_minor = torch.version.cuda.split('.')
    torch_index_url = f"https://download.pytorch.org/whl/cu{cuda_major}{cuda_minor}"
except:
    pass

if sys.version_info < (3, 8):
    import importlib_metadata
else:
    import importlib.metadata as importlib_metadata

device = "cpu"
if torch.cuda.is_available():
    device = "cuda"

try:
    if torch.backends.mps.is_built():
        torch.zeros(1).to(torch.device("mps"))
        device = "mps"
except Exception:
    pass


def actual_install():
    if os.environ.get("PUBLIC_KEY", None):
        print("Docker, returning.")
        db_shared.launch_error = None
        return

    base_dir = os.path.dirname(os.path.realpath(__file__))

    try:
        repo = git.Repo(base_dir)
        revision = repo.rev_parse("HEAD")
    except:
        revision = ""

    print("If submitting an issue on github, please provide the full startup log for debugging purposes.")
    print("")
    print("Initializing Dreambooth")
    print(f"Dreambooth revision: {revision}")

    check_xformers()

    # Only do this if we're using CUDA and not AMD
    if device == "cuda":
        check_bitsandbytes()

    install_requirements()

    check_versions()

    check_torch_unsafe_load()


def pip_install(*args):
    try:
        output = subprocess.check_output(
            [sys.executable, "-m", "pip", "install"] + list(args),
            stderr=subprocess.STDOUT,
        )
        success = False
        for line in output.decode().split("\n"):
            if "Successfully installed" in line:
                print(line)
                success = True
        return success
    except subprocess.CalledProcessError as e:
        print("Error occurred:", e.output.decode())
        return False


def pip_uninstall(*args):
    output = subprocess.check_output(
        [sys.executable, "-m", "pip", "uninstall", "-y"] + list(args),
        stderr=subprocess.STDOUT,
    )
    for line in output.decode().split("\n"):
        if "Successfully uninstalled" in line:
            print(line)


def is_installed(pkg: str, version: Optional[str] = None, check_strict: bool = True) -> bool:
    try:
        # Retrieve the package version from the installed package metadata
        installed_version = metadata.version(pkg)
        print(f"[Installed version of {pkg}: {installed_version}")
        # If version is not specified, just return True as the package is installed
        if version is None:
            return True

        # Compare the installed version with the required version
        if check_strict:
            # Strict comparison (must be an exact match)
            return pv.parse(installed_version) == pv.parse(version)
        else:
            # Non-strict comparison (installed version must be greater than or equal to the required version)
            return pv.parse(installed_version) >= pv.parse(version)

    except metadata.PackageNotFoundError:
        # The package is not installed
        return False
    except Exception as e:
        # Any other exceptions encountered
        print(f"Error: {e}")
        return False


def install_requirements():
    dreambooth_skip_install = os.environ.get("DREAMBOOTH_SKIP_INSTALL", False)
    req_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), "requirements.txt")
    req_file_startup_arg = os.environ.get("REQS_FILE", "requirements_versions.txt")

    if dreambooth_skip_install or req_file == req_file_startup_arg:
        return
    print("Checking Dreambooth requirements...")
    has_diffusers = importlib.util.find_spec("diffusers") is not None
    has_tqdm = importlib.util.find_spec("tqdm") is not None
    transformers_version = importlib_metadata.version("transformers")
    non_strict_separators = ["==", ">=", "<=", ">", "<", "~="]
    # Load the requirements file
    with open(req_file, "r") as f:
        reqs = f.readlines()

    if os.name == "darwin":
        reqs.append("tensorboard==2.11.2")
    else:
        reqs.append("tensorboard>=2.18.0")

    for line in reqs:
        try:
            package = line.strip()
            if package and not package.startswith("#"):
                package_version = None
                strict = "==" in package
                for separator in non_strict_separators:
                    if separator in package:
                        strict = separator == "=="
                        parts = line.split(separator)
                        if len(parts) < 2:
                            print(f"Invalid requirement: {line}")
                            continue
                        package = parts[0].strip()
                        package_version = parts[1].strip()
                        if "#" in package_version:
                            package_version = package_version.split("#")[0]
                        package = package.strip()
                        package_version = package_version.strip()
                        break
                if "#" in package:
                    package = package.split("#")[0]
                package = package.strip()
                v_string = "" if not package_version else f" v{package_version}"
                if not is_installed(package, package_version, strict):
                    print(f"[Dreambooth] {package}{v_string} is not installed.")
                    pip_install(line)
                else:
                    print(f"[Dreambooth] {package}{v_string} is already installed.")

        except subprocess.CalledProcessError as grepexc:
            error_msg = grepexc.stdout.decode()
            print_requirement_installation_error(error_msg)

    # Try importing scipy and numpy, and if they fail, re-install torch
    try:
        import numpy
        import scipy
        import pytorch_lightning
    except ImportError:
        print("Re-installing torch to ensure pytorch_lightning matches torch are installed.")
        tc = torch_command + " --force-reinstall"
        # Remove "pip install " from the command
        tc = tc.replace("pip install ", "pytorch_lightning ")
        pip_install(tc)
        try:
            import numpy
            import scipy
        except ImportError:
            print("Failed to install numpy and scipy after re-installing torch.")
            print("Please install numpy and scipy manually.")
            print("pip install numpy scipy")
            pass

    # Removed outdated transformers version warning that doesn't match requirements.txt


def check_xformers():
    """
    Install xformers if necessary
    """
    print("Checking xformers...")
    try:
        xformers_version = importlib_metadata.version("xformers")
        xformers_outdated = Version(xformers_version) < Version("0.0.27")
        # Parse arguments, see if --xformers is passed
        from modules import shared
        cmd_opts = shared.cmd_opts
        if cmd_opts.xformers or cmd_opts.reinstall_xformers:
            if xformers_outdated:
                print("Xformers is outdated, and automatic installation has been removed. Please install manually if desired.")

    except:
        pass


def check_bitsandbytes():
    """
    Check for "different" B&B Files and copy only if necessary
    """
    print("Checking bitsandbytes...")
    try:
        bitsandbytes_version = importlib_metadata.version("bitsandbytes")
    except:
        bitsandbytes_version = None

    print("Checking bitsandbytes (ALL!)")
    if bitsandbytes_version is None or (bitsandbytes_version and Version(bitsandbytes_version) < Version("0.45.2")):
        try:
            print("Installing bitsandbytes")
            pip_install("bitsandbytes>=0.45.2", "--prefer-binary")
        except:
            print("Bitsandbytes 0.45.2 installation failed")
            print("Some features such as 8bit optimizers will be unavailable")
            print("Install manually with")
            print("'python -m pip install bitsandbytes>=0.45.2  --prefer-binary --force-install'")
            pass


@dataclass
class Dependency:
    module: str
    version: str
    version_comparison: str = "min"
    required: bool = True


def check_versions():
    import platform
    from sys import platform as sys_platform
    is_mac = sys_platform == 'darwin' and platform.machine() == 'arm64'

    dependencies = [
        Dependency(module="accelerate", version="0.21.0"),
        Dependency(module="diffusers", version="0.32.2"),
        Dependency(module="transformers", version="4.49.0")
    ]

    if device == "cuda":
        dependencies.append(Dependency(module="bitsandbytes", version="0.45.2", required=False))

    if device != "mps":
        dependencies.append(Dependency(module="xformers", version="0.0.27", required=False))

    launch_errors = []

    for dependency in dependencies:
        module = dependency.module

        has_module = importlib.util.find_spec(module) is not None
        installed_ver = None
        if has_module:
            try:
                installed_ver = importlib_metadata.version(module)
            except:
                pass

        if not installed_ver:
            module_msg = ""
            cmd_args = sys.argv
            if module != "xformers" and "--xformers" in cmd_args:
                launch_errors.append(f"{module} not installed.")
                module_msg = "(Be sure to use the --xformers flag.)"

            print(f"[!] {module} NOT installed.{module_msg}")
            continue

        required_version = dependency.version
        required_comparison = dependency.version_comparison

        if required_comparison == "min" and Version(installed_ver) < Version(required_version):
            if dependency.required:
                launch_errors.append(f"{module} is below the required {required_version} version.")
            print(f"[!] {module} version {installed_ver} installed.")

        elif required_comparison == "exact" and Version(installed_ver) != Version(required_version):
            launch_errors.append(f"{module} is not the required {required_version} version.")
            print(f"[!] {module} version {installed_ver} installed.")

        else:
            print(f"[+] {module} version {installed_ver} installed.")

    try:
        if len(launch_errors):
            print_launch_errors(launch_errors)
            os.environ["ERRORS"] = json.dumps(launch_errors)
        else:
            os.environ["ERRORS"] = ""
    except Exception as e:
        print(e)


def print_requirement_installation_error(err):
    print("# Requirement installation exception:")
    for line in err.split('\n'):
        line = line.strip()
        if line:
            print(line)


def print_bitsandbytes_installation_error(err):
    print()
    print("#######################################################################################################")
    print("#                                       BITSANDBYTES ISSUE DETECTED                                     #")
    print("#######################################################################################################")
    print("#")
    print("# Dreambooth could not find a compatible version of bitsandbytes.")
    print("# bitsandbytes will not be available for Dreambooth.")
    print("#")
    print("# Bitsandbytes installation exception:")
    for line in err.split('\n'):
        line = line.strip()
        if line:
            print(line)
    print("#")
    print("# TO FIX THIS ISSUE, DO THE FOLLOWING:")
    print("# 1. Fully restart your project (not just the webpage)")
    print("# 2. Running the following commands from the A1111 project root:")
    print("cd venv/Scripts")
    print("activate")
    print("cd ../..")
    print("# WINDOWS ONLY: ")
    print(
        "pip install bitsandbytes>=0.45.2 --prefer-binary --force-reinstall")
    print("#######################################################################################################")


def print_xformers_installation_error(err):
    torch_ver = importlib_metadata.version("torch")
    print()
    print("#######################################################################################################")
    print("#                                       XFORMERS ISSUE DETECTED                                       #")
    print("#######################################################################################################")
    print("#")
    print(f"# Dreambooth could not find a compatible version of xformers (>= 0.0.27 built with torch {torch_ver})")
    print("# xformers will not be available for Dreambooth. Consider upgrading to Torch 2.")
    print("#")
    print("# Xformers installation exception:")
    for line in err.split('\n'):
        line = line.strip()
        if line:
            print(line)
    print("#")
    print("#######################################################################################################")


def print_launch_errors(launch_errors):
    print()
    print("#######################################################################################################")
    print("#                                       LIBRARY ISSUE DETECTED                                        #")
    print("#######################################################################################################")
    print("#")
    print("# " + "\n# ".join(launch_errors))
    print("#")
    print("# Dreambooth may not work properly.")
    print("#")
    print("# TROUBLESHOOTING")
    print("# 1. Fully restart your project (not just the webpage)")
    print("# 2. Update your A1111 project and extensions")
    print("# 3. Dreambooth requirements should have installed automatically, but you can manually install them")
    print("#    by running the following commands from the A1111 project root:")
    print("cd venv/Scripts")
    print("activate")
    print("cd ../..")
    print("pip install -r ./extensions/sd_dreambooth_extension/requirements.txt")
    print(
        "pip install bitsandbytes>=0.45.2 --prefer-binary --force-reinstall")
    print("#######################################################################################################")


def check_torch_unsafe_load():
    try:
        from modules import safe
        safe.load = safe.unsafe_torch_load
        import torch
        torch.load = safe.unsafe_torch_load
    except:
        pass


def print_xformers_torch1_instructions(xformers_version):
    print(f"# Your version of xformers is {xformers_version}.")
    print("# xformers >= 0.0.27 is required to be available on the Dreambooth tab.")
    print("# Torch 1 wheels of xformers >= 0.0.27 are no longer available on PyPI,")
    print("# but you can manually download them by going to:")
    print("https://github.com/facebookresearch/xformers/actions")
    print("# Click on the most recent action tagged with a release (middle column).")
    print("# Select a download based on your environment.")
    print("# Unzip your download")
    print("# Activate your venv and install the wheel: (from A1111 project root)")
    print("cd venv/Scripts")
    print("activate")
    print("pip install {REPLACE WITH PATH TO YOUR UNZIPPED .whl file}")
    print("# Then restart your project.")
