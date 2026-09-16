#!/usr/bin/env python3
"""Change the public origin using customer-owned Secrets Manager and SSM."""
import argparse,json,pathlib,shlex,subprocess,tempfile,time
from deploy_ec2_ssm import deployment_instances
p=argparse.ArgumentParser();p.add_argument('--work',type=pathlib.Path,required=True);p.add_argument('--origin',required=True);a=p.parse_args()
outputs=json.loads((a.work/'outputs.json').read_text());config=json.loads((a.work/'config.json').read_text())
def aws(*args):return json.loads(subprocess.check_output(['aws',*args,'--output','json','--region',config['region']],text=True))
if outputs['compute_mode']['value']!='ec2':raise ValueError('URL migration currently requires the supported EC2 tiers')
# Use the in-container bootstrap identity. Neither its password nor its admin
# token travels in the SSM request, output, or local Terraform state.
redirects=json.dumps([a.origin+'/oauth/callback','relay://oauth/callback'])
origins=json.dumps([a.origin])
script='''set -eu
umask 077
kc=/opt/keycloak/bin/kcadm.sh
cfg=$(mktemp /tmp/relay-kcadm.XXXXXX)
trap 'rm -f "$cfg"' EXIT
"$kc" config credentials --config "$cfg" --server http://127.0.0.1:8080/auth --realm master --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null 2>&1
id=$("$kc" get clients --config "$cfg" -r relay -q clientId=relay --fields id --format csv --noquotes)
test -n "$id"
"$kc" update "clients/$id" --config "$cfg" -r relay -s '''+shlex.quote('redirectUris='+redirects)+' -s '+shlex.quote('webOrigins='+origins)+' -s '+shlex.quote('attributes."post.logout.redirect.uris"='+a.origin+'/*##relay://oauth/logout')+' >/dev/null\n'
command='set -eu\nid=$(docker ps -q --filter label=com.docker.compose.service=keycloak)\ntest "$(printf "%s\\n" "$id" | wc -l)" -eq 1\ntest -n "$id"\ndocker exec "$id" sh -ec '+shlex.quote(script)
instances=deployment_instances(outputs,config['region'])
with tempfile.NamedTemporaryFile(mode='w') as f:
 json.dump({'commands':[command],'executionTimeout':['120']},f);f.flush()
 result=aws('ssm','send-command','--document-name','AWS-RunShellScript','--instance-ids',*instances,'--parameters','file://'+f.name,'--comment','Update customer Relay public origin')
command_id=result['Command']['CommandId'];deadline=time.monotonic()+180
while time.monotonic()<deadline:
 statuses=aws('ssm','list-command-invocations','--command-id',command_id)['CommandInvocations']
 if len(statuses)==len(instances) and all(x['Status']=='Success' for x in statuses):break
 if any(x['Status'] in ['Failed','TimedOut','Cancelled','Undeliverable','Terminated'] for x in statuses):raise RuntimeError('Keycloak URL update failed; inspect the SSM command in your AWS account')
 time.sleep(3)
else:raise TimeoutError('Keycloak URL update timed out')
for output,updates in [('runtime_secret_arn',{'KEYCLOAK_ISSUER':a.origin+'/auth/realms/relay','RELAY_SESSION_ISSUER':a.origin,'CLIENT_ORIGIN':a.origin}),('keycloak_secret_arn',{'KC_HOSTNAME':a.origin+'/auth'})]:
 arn=outputs[output]['value'];data=json.loads(aws('secretsmanager','get-secret-value','--secret-id',arn)['SecretString']);data.update(updates)
 with tempfile.NamedTemporaryFile(mode='w') as f:
  json.dump(data,f);f.flush();aws('secretsmanager','put-secret-value','--secret-id',arn,'--secret-string','file://'+f.name)
outputs['application_url']['value']=a.origin;outputs['keycloak_url']['value']=a.origin+'/auth';outputs['keycloak_admin_console_url']['value']=a.origin+'/auth/admin/relay/console/'
(a.work/'outputs.json').write_text(json.dumps(outputs,indent=2)+'\n')
print('Updated runtime origins and the existing Keycloak client. The installer will now reconcile the running services.')
