"""Disposable two-customer records produced by the offline journal API."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import x07_receipt_history_audit as audit
from x07_profile_workflow import ProfileWorkflow


def seed(root):
    profile=Path(root)/'profile.sqlite'; journal=Path(root)/'journal.sqlite'
    store=audit.CustomerProfileStore(profile); receipts=audit.ConfirmationReceiptStore(journal)
    pins={}
    for customer in ['a','b']:
        saved=store.save_version(customer,{'module':{'block':'PRIVATE_'+customer,'layer':'M'}},expected_version=0)
        project='old-'+customer; pin=store.apply_to_project(project,customer); pins[customer]=pin
        context={'customer_key':customer,'project_key':project,'drawing_key':'drawing-'+customer}
        result=ProfileWorkflow._result(context,pin,saved['roles'],{'count':1})
        receipts.run(context,'0'*64,lambda:result,lambda value:None)
    return profile,journal,audit.CustomerProfileStore(profile,read_only=True),audit.ConfirmationReceiptStore(journal,read_only=True),pins
