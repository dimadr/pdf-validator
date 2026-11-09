"""Owner/date gate. This module never performs deployment or changes state."""
import argparse,datetime,sys
from zoneinfo import ZoneInfo
# Owner's explicit GO on 02.11.2026 supersedes the previous 03.11 boundary.
EARLIEST=datetime.date(2026,11,2)
APPROVAL='GO: production deployment'
def permitted(approval,instant):
    if instant.astimezone(ZoneInfo('Europe/Moscow')).date()<EARLIEST:
        return False,'STOP: production must remain unchanged before 02.11.2026 Europe/Moscow'
    if approval!=APPROVAL:return False,'STOP: separate owner GO: production deployment is missing'
    return True,'Owner/date gate satisfied; all technical preflight checks are still mandatory'
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--approval',default='');args=parser.parse_args()
    allowed,message=permitted(args.approval,datetime.datetime.now(datetime.timezone.utc))
    print(message);return 0 if allowed else 2
if __name__=='__main__':sys.exit(main())
