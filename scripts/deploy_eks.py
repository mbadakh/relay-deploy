#!/usr/bin/env python3
import argparse,hashlib,json,os,pathlib,subprocess,uuid
p=argparse.ArgumentParser();p.add_argument('--leaf',type=pathlib.Path,required=True);p.add_argument('--terraform-output',type=pathlib.Path,required=True);p.add_argument('--region',required=True);a=p.parse_args()
outputs=json.loads(a.terraform_output.read_text());out=lambda key:outputs[key]['value']
os.environ['AWS_REGION']=a.region;os.environ['RELAY_CUSTOMER_SLUG']=out('customer_slug')
repo=pathlib.Path(__file__).resolve().parent.parent
revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
digest=hashlib.sha256(a.terraform_output.read_bytes()).hexdigest();key='deploy-context/'+uuid.uuid4().hex+'.json'
r=subprocess.run(['aws','s3api','put-object','--bucket',out('bucket_names')['config'],'--key',key,'--body',str(a.terraform_output),'--server-side-encryption','aws:kms','--ssekms-key-id',out('customer_kms_key_arn'),'--metadata','sha256='+digest,'--output','json'],capture_output=True,text=True,check=True)
version=json.loads(r.stdout)['VersionId']
subprocess.run(['python3',str(repo/'scripts/deploy_eks_operation.py'),'--repo',str(repo),'--leaf',str(a.leaf),'--terraform-output',str(a.terraform_output),'--region',a.region,'--revision',revision,'--context-key',key,'--context-version',version,'--context-sha256',digest,'--run-token',str(uuid.uuid4())],check=True)
