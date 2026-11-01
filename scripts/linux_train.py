"""Train or verify the explicitly selected Linux WPS pilot candidate."""
import argparse
import json
from pathlib import Path
from classification.linux_pilot import train_pilot, verify_candidate


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    train=commands.add_parser("train");train.add_argument("--intake",type=Path,required=True);train.add_argument("--output",type=Path,required=True)
    verify=commands.add_parser("verify");verify.add_argument("--model",type=Path,required=True)
    args=parser.parse_args(argv)
    if args.command=="verify":
        model,_=verify_candidate(args.model)
        print(json.dumps({"status":"verified","model_id":model["model_id"],"native_runtime_ready":False}))
    else:
        result=train_pilot(args.intake,args.output)
        print(json.dumps({"model":str(args.output.resolve()/"model/model.json"),"training_windows":result["training_windows"],
            "validation_accuracy":result["groups"]["independent/validation"]["accuracy"],
            "test_diagnostic_accuracy":result["groups"]["independent/test"]["accuracy"],
            "continuous_accuracy":result["continuous_overall"]["accuracy"]}))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
