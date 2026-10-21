"""Verify an external 190-observation WPS pilot without changing full-batch gates."""
from pathlib import Path
import argparse
import json

from collector.linux.intake import intake_pilot


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--build",type=Path,default=Path("build/native"))
    args=parser.parse_args()
    report=intake_pilot(args.archive,args.output,args.build.resolve(),Path(__file__).resolve().parents[1])
    print(json.dumps({key:report[key] for key in ("status","selected_formal_windows","separate_preflight_windows","frozen_native_metrics")},ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
