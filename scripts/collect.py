"""Explicit application selection; only this command starts collection."""
import argparse
import importlib


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--app', required=True, choices=['bilibili', 'huawei_browser', 'wps'])
    p.add_argument('--describe', action='store_true', help='Show implementation module without contacting device')
    a, rest = p.parse_known_args()
    module = 'collector.wps.collect' if a.app == 'wps' else f'apps.{a.app}.collector'
    if a.describe:
        print(module)
        return 0
    return importlib.import_module(module).main(rest)


if __name__ == '__main__':
    raise SystemExit(main())
