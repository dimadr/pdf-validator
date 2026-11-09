"""Create an immutable-operation handover plan, never execute it."""
import argparse,json,pathlib
from queue_policy import prepare,write_artifact
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('inventory','classification','output'):p.add_argument('--'+name,type=pathlib.Path,required=True)
    a=p.parse_args();result=prepare(json.loads(a.inventory.read_text()),json.loads(a.classification.read_text()));write_artifact(a.output,result)
    print(json.dumps({'planned_operations':len(result['operations']),'automatic_replay':0}))
