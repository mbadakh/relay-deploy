#!/usr/bin/env python3
"""Render the standalone Compose file from reviewed immutable image locks."""
import json,pathlib,yaml
root=pathlib.Path(__file__).resolve().parent.parent
release=json.loads((root/'release.lock.json').read_text());platform=json.loads((root/'platform-images.lock.json').read_text())['images']
image=lambda item:item['source']+'@'+item['digest']
base={'restart':'unless-stopped','logging':{'driver':'json-file','options':{'max-size':'10m','max-file':'3'}}}
def service(**kw):return {**base,**kw}
config_mount={'type':'volume','source':'config','target':'/run/relay','read_only':True}
bootstrap_mount={'type':'bind','source':'./local','target':'/bootstrap','read_only':True}
services={
 'init':{'image':(root/'tools-image.txt').read_text().strip(),'platform':'linux/amd64','user':'0:0','command':['python3','/bootstrap/init.py'],'environment':{'RELAY_TERMINAL_RELEASE_READY':str(release.get('terminalReady',False)).lower(),'RELAY_PUBLIC_URL':'${RELAY_PUBLIC_URL:-http://localhost:8080}','RELAY_ADMIN_EMAIL':'${RELAY_ADMIN_EMAIL:-admin@example.invalid}','RELAY_TURN_URL':'${RELAY_TURN_URL:-turn:127.0.0.1:3478?transport=udp}'},'volumes':['config:/run/relay','storage_data:/storage',bootstrap_mount],'restart':'no'},
 'db':service(image=image(platform['postgres']),environment={'POSTGRES_USER':'relay_admin','POSTGRES_DB':'relay','POSTGRES_PASSWORD_FILE':'/run/relay/db-password'},entrypoint=['sh','-ec','cp /run/relay/database.sql /docker-entrypoint-initdb.d/relay.sql; chmod 644 /docker-entrypoint-initdb.d/relay.sql; exec docker-entrypoint.sh postgres'],volumes=['db_data:/var/lib/postgresql/data',config_mount],depends_on={'init':{'condition':'service_completed_successfully'}},healthcheck={'test':['CMD-SHELL','pg_isready -U relay_admin -d relay'],'interval':'5s','timeout':'5s','retries':30}),
 'storage':service(image='minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e',user='1000:1000',entrypoint=['/bin/sh','/bootstrap/entrypoint.sh','storage','minio','server','/data'],volumes=['storage_data:/data',config_mount,bootstrap_mount],depends_on={'init':{'condition':'service_completed_successfully'}}),
 'keycloak':service(image=image(release['images']['keycloak']),entrypoint=['/bin/sh','/bootstrap/entrypoint.sh','keycloak','/opt/keycloak/bin/kc.sh','start','--import-realm'],volumes=[config_mount,bootstrap_mount,{'type':'volume','source':'config','target':'/opt/keycloak/data/import','read_only':True}],depends_on={'db':{'condition':'service_healthy'}}),
 'relay':service(image=image(release['images']['server']),entrypoint=['/bin/sh','/bootstrap/entrypoint.sh','relay','node','server/index.js'],environment={'NODE_ENV':'production'},volumes=['uploads:/app/uploads',config_mount,bootstrap_mount],depends_on={'db':{'condition':'service_healthy'},'storage':{'condition':'service_started'},'keycloak':{'condition':'service_started'}},healthcheck={'test':['CMD','node','-e',"fetch('http://127.0.0.1:3001/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"],'interval':'10s','timeout':'5s','retries':30}),
 'web':service(image=image(platform['caddy']),ports=['${RELAY_BIND_ADDRESS:-127.0.0.1}:${RELAY_HTTP_PORT:-8080}:80'],volumes=['./local/Caddyfile:/etc/caddy/Caddyfile:ro','caddy_data:/data','caddy_config:/config'],depends_on={'relay':{'condition':'service_healthy'}}),
 'turn':service(image=image(platform['coturn']),command=['-c','/run/relay/turn.conf'],volumes=[config_mount],ports=['${RELAY_BIND_ADDRESS:-127.0.0.1}:3478:3478/udp','${RELAY_BIND_ADDRESS:-127.0.0.1}:3478:3478/tcp','${RELAY_BIND_ADDRESS:-127.0.0.1}:49160-49200:49160-49200/udp'],depends_on={'init':{'condition':'service_completed_successfully'}}),
}
(root/'compose.yaml').write_text(yaml.safe_dump({'name':'relay-self-hosted','services':services,'volumes':{n:{} for n in ['config','db_data','storage_data','uploads','caddy_data','caddy_config']}},sort_keys=False))
