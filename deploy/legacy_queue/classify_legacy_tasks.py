"""Offline classification; no network or state changes."""
import argparse,json,pathlib
from queue_policy import classify,write_artifact
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--inventory',type=pathlib.Path,required=True);p.add_argument('--output',type=pathlib.Path,required=True);a=p.parse_args()
    result=classify(json.loads(a.inventory.read_text()));write_artifact(a.output,result)
    print(json.dumps({k:result[k] for k in ('physical_messages','unique_task_ids','unique_documents','summary')}))
