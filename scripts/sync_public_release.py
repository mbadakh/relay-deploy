#!/usr/bin/env python3
"""Adopt the published public image release; no GitHub authentication needed."""
import json,pathlib,re,subprocess
root=pathlib.Path(__file__).resolve().parent.parent
run=lambda *args:subprocess.check_output(args,text=True).strip()
source='public.ecr.aws/m0z0w6q1/relay-server'
digest=run('crane','digest',source+':latest')
config=json.loads(run('crane','config','--platform','linux/amd64',source+'@'+digest))
labels=config['config']['Labels']
if labels.get('dev.r3l4y.terminal-deployment')!='1':raise ValueError('This release predates the terminal deployment integration')
version=labels['org.opencontainers.image.version'];commit=labels['org.opencontainers.image.revision']
if not re.fullmatch(r'\d+\.\d+\.\d+',version) or not re.fullmatch(r'[a-f0-9]{40}',commit):raise ValueError('Missing production version provenance')
if run('crane','digest',source+':'+version)!=digest:raise ValueError('Version and latest tags disagree')
keycloak='public.ecr.aws/m0z0w6q1/relay-keycloak';kc_digest=run('crane','digest',keycloak+':'+version)
lock=json.loads((root/'release.lock.json').read_text())
lock.update(version=version,sourceCommit=commit,scaleReady=False,terminalReady=True)
lock['images']={'server':{'source':source,'digest':digest},'keycloak':{'source':keycloak,'digest':kc_digest}}
lock['clients']['android']['pushTransport'].update(customerBackendRequiresMatchingProjectCredentials=False,gatewayUrl='https://push.r3l4y.dev',deviceEnrollmentRequired=True)
lock['clients']['android']['limitation']='Customer background notifications require public HTTPS and device enrollment with the official Relay push gateway.'
(root/'release.lock.json').write_text(json.dumps(lock,indent=2)+'\n')
subprocess.run(['python3',str(root/'scripts/refresh_images.py')],check=True)
print('Pinned public Relay release',version,'at',digest)
