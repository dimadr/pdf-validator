"""Read-only PLAN_READY/CUTOVER_READY/STOP gate, or receipt of a copied escrow."""
import argparse,hashlib,json,pathlib,socket
from queue_policy import verify,write_artifact
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--inventory',type=pathlib.Path);p.add_argument('--plan',type=pathlib.Path)
    p.add_argument('--mode',choices=('planned','drain','drained','fenced'),default='planned');p.add_argument('--escrow',type=pathlib.Path);p.add_argument('--peer-receipt',type=pathlib.Path)
    p.add_argument('--record-copy',type=pathlib.Path);p.add_argument('--output',type=pathlib.Path,required=True);a=p.parse_args()
    if a.record_copy:result={'sha256':hashlib.sha256(a.record_copy.read_bytes()).hexdigest(),'host':socket.gethostname(),'path':str(a.record_copy.resolve())}
    else:
        if not a.inventory or not a.plan:p.error('--inventory and --plan are required')
        snapshot=json.loads(a.inventory.read_text())
        if a.escrow and not snapshot.get('escrow_sha256'):snapshot['escrow_sha256']=hashlib.sha256(a.escrow.read_bytes()).hexdigest()
        result=verify(snapshot,json.loads(a.plan.read_text()),a.mode,
            a.escrow.read_bytes() if a.escrow else None,json.loads(a.peer_receipt.read_text()) if a.peer_receipt else None,socket.gethostname())
    write_artifact(a.output,result);print(json.dumps(result));raise SystemExit(2 if result.get('verdict')=='STOP' else 0)
