"""Build the C11 device collector without connecting a device."""
import argparse
import os
from pathlib import Path
import subprocess


def build(sdk, output):
    sdk = Path(sdk).expanduser().resolve()
    output = Path(output).resolve()
    clang = sdk / 'llvm' / 'bin' / ('clang.exe' if os.name == 'nt' else 'clang')
    sysroot = sdk / 'sysroot'
    if not clang.is_file() or not sysroot.is_dir():
        raise FileNotFoundError('Need an OpenHarmony native SDK with llvm/bin/clang and sysroot')
    source = Path(__file__).with_name('device') / 'collect.c'
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(clang), '-O2', '-std=c11', '-Wall', '-Wextra',
                    '-target', 'aarch64-linux-ohos', f'--sysroot={sysroot}',
                    '-o', str(output), str(source)], check=True)
    return output


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sdk', default=os.environ.get('OHOS_SDK'))
    p.add_argument('--output', required=True)
    a = p.parse_args()
    if not a.sdk:
        p.error('Pass --sdk or set OHOS_SDK; no SDK is bundled')
    print(build(a.sdk, a.output))


if __name__ == '__main__':
    main()
