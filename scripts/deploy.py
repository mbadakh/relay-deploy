#!/usr/bin/env python3
"""Customer-operated Terraform lifecycle. No platform account or GitHub API."""
from __future__ import annotations
import argparse,fcntl,hashlib,json,os,pathlib,re,subprocess,sys,time,urllib.parse
import yaml
from relay_customers import normalize_slug,normalize_domain
from report import write_report

ROOT=pathlib.Path(__file__).resolve().parent.parent
REGIONS=['us-east-1','us-west-2','eu-central-1','eu-west-1','ap-southeast-1']
TIERS={'10':'10','100':'100','1000':'1k','1k':'1k','10000':'10k','10k':'10k','100000':'100k','100k':'100k','1000000':'1m','1m':'1m'}
def run(*args,capture=False,cwd=None):
    return subprocess.run([str(x) for x in args],cwd=cwd or ROOT,check=True,text=True,stdout=subprocess.PIPE if capture else None).stdout
def aws(*args):return json.loads(run('aws',*args,'--output','json','--no-cli-pager',capture=True))
def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n');path.chmod(0o600)
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def script(name,*args):run('python3',ROOT/'scripts'/name,*args)
def ask(label,default='',choices=None):
    answer=input(f'{label}'+(f' [{default}]' if default else '')+': ').strip() or default
    if choices and answer not in choices:raise ValueError('Choose one of: '+', '.join(choices))
    return answer
def safe_origin(value):
    if not value:return ''
    u=urllib.parse.urlsplit(value)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.path not in ['', '/'] or u.query or u.fragment:raise ValueError('Public URL must be an HTTPS origin without a path or credentials')
    return value.rstrip('/')
def configure(args,identity,release):
    interactive=not args.non_interactive and sys.stdin.isatty()
    home=args.home_lab
    if home is None:home=ask('Is this a home lab? (yes/no)','yes',['yes','no'])=='yes' if interactive else False
    name=args.name or (ask('Server name','my-relay') if interactive else 'my-relay')
    region=args.region or (ask('AWS region','eu-central-1',REGIONS) if interactive else 'eu-central-1')
    users='100' if home else args.users or (ask('Expected registered users (10, 100, 1k, 10k, 100k, 1m)','100',list(TIERS)) if interactive else '100')
    if users not in TIERS:raise ValueError('Unsupported user tier')
    tier=TIERS[users]
    if tier in ['10k','100k','1m'] and release.get('scaleReady') is not True:
        raise ValueError('This release is not certified for distributed large-tier operation. Choose up to 1k registered users; capacity is not a concurrency guarantee.')
    email=args.admin_email or (ask('Initial administrator email','admin@example.invalid') if interactive else 'admin@example.invalid')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or len(email)>254:raise ValueError('Invalid administrator email')
    public=args.public_url or (ask('Public HTTPS URL (optional; your existing reverse proxy can provide it)') if interactive else '')
    public=safe_origin(public)
    zone=args.zone_id or (ask('Your Route 53 zone ID (optional; creates DNS and managed TLS)') if interactive and public else '')
    if zone and (not public or not re.fullmatch(r'Z[A-Z0-9]+',zone)):raise ValueError('A Route 53 zone requires a public HTTPS URL and a valid zone ID')
    if region not in REGIONS:raise ValueError('Unsupported AWS region')
    slug=normalize_slug(name)
    return {'name':name,'slug':slug,'region':region,'users':tier,'home_lab':home,'admin_email':email,'public_origin':public,'infrastructure_domain':urllib.parse.urlsplit(public).hostname or slug+'.invalid','zone_id':zone,'account_id':identity['Account'],'version':release['version']}

def operator_arn(identity):
    arn=identity['Arn']
    if ':assumed-role/' in arn:
        role_name=arn.split(':assumed-role/',1)[1].split('/')[0]
        return aws('iam','get-role','--role-name',role_name)['Role']['Arn']
    if ':user/' in arn or ':role/' in arn:return arn
    raise ValueError('Use an IAM user, role or SSO session, not the AWS root identity')

def terraform_files(work,config,identity):
    tf=work/'terraform';tf.mkdir(exist_ok=True)
    fields=re.findall(r'^output "([^"]+)"', (ROOT/'modules/relay-stack/outputs.tf').read_text(),re.M)
    main='module "relay" {\n source = "../../../modules/relay-stack"\n'
    values={'aws_region':config['region'],'customer_name':config['name'],'environment':'production','capacity_profile':config['users'],'home_lab':config['home_lab'],'domain_name':config.get('infrastructure_domain') or urllib.parse.urlsplit(config['public_origin']).hostname or config['slug']+'.invalid','hosted_zone_id':config['zone_id'],'public_origin':config['public_origin'],'operator_principal_arn':operator_arn(identity),'relay_app_replicas':1,'scale_ready_release':False,'deletion_protection':True,'offboarding':config.get('purge_data',False)}
    platform=json.loads((ROOT/'platform-images.lock.json').read_text())['images']
    values.update(turn_image_digest=platform['coturn']['digest'],caddy_image_digest=platform['caddy']['digest'],postgres_image_digest=platform['postgres']['digest'])
    for key,value in values.items():main+=f' {key} = {json.dumps(value)}\n'
    main+='}\n'
    for field in fields:main+=f'output "{field}" {{\n value = module.relay.{field}\n sensitive = true\n}}\n'
    (tf/'main.tf').write_text(main)
    return tf

def render_leaf(work,config,outputs,release):
    out=lambda key:outputs[key]['value']
    leaf=work/'leaf';leaf.mkdir(exist_ok=True)
    origin=out('application_url');registry=out('ecr_repository_urls')['relay-server'].split('/')[0]
    platform=json.loads((ROOT/'platform-images.lock.json').read_text())['images']
    substitutions={'CUSTOMER_SLUG':out('customer_slug'),'AWS_REGION':config['region'],'DOMAIN_NAME':urllib.parse.urlsplit(origin).hostname,'PUBLIC_ORIGIN':origin,'ECR_REGISTRY':registry,'SERVER_IMAGE_DIGEST':release['images']['server']['digest'],'KEYCLOAK_IMAGE_DIGEST':release['images']['keycloak']['digest'],'KEYCLOAK_IMAGE_ID':release['images']['keycloak']['digest'].split(':')[1][:16],'CADDY_SITE':origin if config['public_origin'] and config['zone_id'] else ':80','DATABASE_HOST':out('database_endpoint'),'RELAY_REPLICAS':'1','KEYCLOAK_REPLICAS':'1','SCALE_READY_RELEASE':'false'}
    for key,val in platform.items():substitutions[key.upper()+'_IMAGE_DIGEST']=val['digest']
    substitutions['TURN_IMAGE_DIGEST']=platform['coturn']['digest']
    templates={'compose.yaml':'compose-homelab.yaml.tmpl' if config['home_lab'] else 'compose.yaml.tmpl','keycloak-realm.json':'keycloak-realm.json.tmpl','Caddyfile':'Caddyfile.tmpl','values.yaml':'values.yaml.tmpl'}
    for name,template in templates.items():
        text=(ROOT/'templates/customer'/template).read_text()
        for key,value in substitutions.items():text=text.replace('{{'+key+'}}',str(value))
        if '{{' in text:raise ValueError('Unresolved template variables: '+template)
        (leaf/name).write_text(text)
    realm=json.loads((leaf/'keycloak-realm.json').read_text())
    realm['sslRequired']='external' if origin.startswith('https:') else 'none'
    save(leaf/'keycloak-realm.json',realm)
    values=yaml.safe_load((leaf/'values.yaml').read_text());values['customer']['publicOrigin']=origin
    (leaf/'values.yaml').write_text(yaml.safe_dump(values,sort_keys=False))
    save(leaf/'release.lock.json',release)
    script('render_runtime_values.py','--leaf',leaf,'--terraform-output',work/'outputs.json')
    return leaf

def deploy_runtime(work,config,outputs,release):
    leaf=render_leaf(work,config,outputs,release)
    script('seed_secrets.py','--terraform-output',work/'outputs.json','--domain',urllib.parse.urlsplit(outputs['application_url']['value']).hostname,'--region',config['region'],'--admin-email',config['admin_email'])
    script('promote_images.py','--release-lock',leaf/'release.lock.json','--platform-images',ROOT/'platform-images.lock.json','--terraform-output',work/'outputs.json','--region',config['region'])
    if outputs['compute_mode']['value']=='ec2':
        script('bootstrap_database.py','--terraform-output',work/'outputs.json','--topology','ec2','--region',config['region'])
        script('reconcile_turn_ssm.py','--terraform-output',work/'outputs.json','--region',config['region'])
        script('deploy_ec2_ssm.py','--leaf',leaf,'--terraform-output',work/'outputs.json','--region',config['region'],'--source-revision',release['sourceCommit'])
    else:script('deploy_eks.py','--leaf',leaf,'--terraform-output',work/'outputs.json','--region',config['region'])
    print('\nDeployment complete.')
    print('Public endpoint:',outputs['public_endpoint']['value'])
    print('Application URL:',outputs['application_url']['value'])
    print('Administrator: relay-admin. Retrieve your temporary password from AWS Secrets Manager:')
    print(outputs['initial_relay_admin_credentials_secret_arn']['value'])
    if not outputs['application_url']['value'].startswith('https:'):
        print('Configure a trusted HTTPS reverse proxy before connecting the official mobile app. Run configure-url with your HTTPS origin afterwards.')
    print('TURN:',outputs['turn_url']['value'])
    print('State and recovery files:',display_path(work))

def display_path(path):return str(path).replace(str(ROOT),os.environ.get('RELAY_HOST_DIRECTORY',str(ROOT)),1)
def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',nargs='?',default='deploy',choices=['deploy','plan','resume','status','admin','destroy','configure-url'])
    p.add_argument('--name');p.add_argument('--region');p.add_argument('--users',choices=list(TIERS));p.add_argument('--home-lab',action='store_true',default=None)
    p.add_argument('--production',action='store_false',dest='home_lab');p.add_argument('--admin-email');p.add_argument('--public-url');p.add_argument('--zone-id');p.add_argument('--profile');p.add_argument('--non-interactive',action='store_true');p.add_argument('--version');p.add_argument('--purge-data',action='store_true')
    args=p.parse_args()
    if args.profile:os.environ['AWS_PROFILE']=args.profile
    release=json.loads((ROOT/'release.lock.json').read_text())
    if args.version and args.version!=release['version']:raise ValueError('Requested version does not match this checkout. Check out the deployment release with the desired image lock.')
    if args.command=='deploy' and release.get('terminalReady') is not True:raise ValueError('The compatible production image is awaiting approved public publishing. You can use the plan command to review infrastructure and pricing now.')
    identity=aws('sts','get-caller-identity');print('AWS account:',identity['Account'],'Identity:',identity['Arn'])
    if args.command in ['deploy','plan']:
        config=configure(args,identity,release)
    else:
        if not args.name:raise ValueError('--name is required for an existing deployment')
        config=json.loads((ROOT/'.relay'/normalize_slug(args.name)/'config.json').read_text())
        if config['account_id']!=identity['Account']:raise ValueError('AWS account does not match this saved deployment')
    os.environ['AWS_REGION']=config['region'];os.environ['AWS_DEFAULT_REGION']=config['region']
    work=ROOT/'.relay'/config['slug'];work.mkdir(parents=True,exist_ok=True);work.chmod(0o700)
    if args.command not in ['deploy','plan'] and (work/'release.lock.json').exists():
        release=json.loads((work/'release.lock.json').read_text())
    with (work/'operation.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        existing=work/'config.json'
        if existing.exists():
            old=json.loads(existing.read_text())
            config['infrastructure_domain']=old.get('infrastructure_domain') or urllib.parse.urlsplit(old['public_origin']).hostname or old['slug']+'.invalid'
            if old['account_id']!=config['account_id'] or old['region']!=config['region']:raise ValueError('Cannot change the account or region of saved state. Use a new server name.')
        save(existing,config)
        tf=terraform_files(work,config,identity)
        if args.command in ['status','admin']:
            outputs=json.loads((work/'outputs.json').read_text())
            if args.command=='status':print(json.dumps({k:outputs[k]['value'] for k in ['application_url','public_endpoint','compute_mode']},indent=2))
            else:
                print('This displays the initial secret on your own terminal.')
                if input('Type SHOW to continue: ')!='SHOW':return
                secret=aws('secretsmanager','get-secret-value','--secret-id',outputs['initial_relay_admin_credentials_secret_arn']['value'])
                value=json.loads(secret['SecretString']);print('Username:',value['RELAY_INITIAL_ADMIN_USERNAME']);print('Temporary password:',value['RELAY_INITIAL_ADMIN_PASSWORD'])
            return
        if args.command=='configure-url':
            if not args.public_url:raise ValueError('--public-url is required')
            config['public_origin']=safe_origin(args.public_url);save(existing,config)
            script('configure_url.py','--work',work,'--origin',config['public_origin'])
            terraform_files(work,config,identity)
            outputs=json.loads((work/'outputs.json').read_text());deploy_runtime(work,config,outputs,release);return
        if args.command=='resume':
            outputs=json.loads(run('terraform',f'-chdir={tf}','output','-json',capture=True))
            if config['public_origin']:
                outputs['application_url']['value']=config['public_origin']
            save(work/'outputs.json',outputs)
            deploy_runtime(work,config,outputs,release);return
        run('terraform',f'-chdir={tf}','init','-input=false','-no-color')
        if args.command=='destroy':
            if not args.purge_data:raise ValueError('Export and verify your backups first. Destruction requires --purge-data and two explicit plan approvals; it removes the database, media, volumes and managed backups.')
            if input('Type PURGE '+config['slug']+' to acknowledge permanent data loss: ')!='PURGE '+config['slug']:return
            config['purge_data']=True;save(existing,config);terraform_files(work,config,identity)
            for stage,extra in [('prepare-removal',[]),('destroy',['-destroy'])]:
                deletion_plan=work/(stage+'.tfplan')
                run('terraform',f'-chdir={tf}','plan','-input=false','-no-color',*extra,f'-out={deletion_plan}')
                checksum=sha(deletion_plan);review_file=work/(stage+'.txt')
                review_file.write_text(run('terraform',f'-chdir={tf}','show','-no-color',deletion_plan,capture=True))
                print('Review:',display_path(review_file))
                if input('Type APPLY '+checksum[:12]+' to approve this exact removal plan: ')!='APPLY '+checksum[:12]:return
                if sha(deletion_plan)!=checksum:raise ValueError('Removal plan changed after review')
                run('terraform',f'-chdir={tf}','apply','-input=false','-no-color',deletion_plan)
            print('Managed resources removed. KMS keys and secrets use their configured recovery windows. Independently exported backups remain yours.');return
        plan=work/'deployment.tfplan'
        run('terraform',f'-chdir={tf}','plan','-input=false','-no-color',f'-out={plan}')
        digest=sha(plan)
        plan_json=work/'plan.json';plan_json.write_text(run('terraform',f'-chdir={tf}','show','-json',plan,capture=True));plan_json.chmod(0o600)
        customer={'customer_slug':config['slug'],'aws_region':config['region'],'expected_users':config['users'],'home_lab':config['home_lab']};(work/'customer.yaml').write_text(yaml.safe_dump(customer))
        script('create_cost_report.py','--plan',plan_json,'--plan-sha256',digest,'--customer',work/'customer.yaml','--markdown',work/'pricing.md','--json',work/'pricing.json')
        pricing=json.loads((work/'pricing.json').read_text());review=work/'review.html'
        write_report(review,config,json.loads(plan_json.read_text()),pricing,digest)
        print('\nEstimated monthly total: $'+pricing['summary']['monthlyTotal']+' USD')
        print('Detailed plan and pricing report:',display_path(review))
        print('No AWS resources from this plan have been applied.')
        if args.command=='plan':return
        confirmation='APPLY '+digest[:12]
        if input(f'Review the HTML report. Type {confirmation} to approve this exact plan: ').strip()!=confirmation:
            print('Cancelled. No resources applied.');return
        if sha(plan)!=digest:raise ValueError('Saved plan changed after pricing; approval is invalid')
        save(work/'approval.json',{'planSha256':digest,'pricingSha256':sha(work/'pricing.json'),'approvedAt':time.time(),'accountId':identity['Account']})
        save(work/'release.lock.json',release)
        run('terraform',f'-chdir={tf}','apply','-input=false','-no-color',plan)
        outputs=json.loads(run('terraform',f'-chdir={tf}','output','-json',capture=True));save(work/'outputs.json',outputs)
        deploy_runtime(work,config,outputs,release)

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,subprocess.CalledProcessError,EOFError) as e:
        print('Error:',e,file=sys.stderr);print('Your state is preserved under .relay/. After resolving the cause, use ./relay resume --name SERVER for an already-applied deployment.',file=sys.stderr);sys.exit(1)
