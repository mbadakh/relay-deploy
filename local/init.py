import json,os,secrets,pathlib,urllib.parse
if os.environ.get('RELAY_TERMINAL_RELEASE_READY')!='true':
    raise SystemExit('Private preview: the compatible production image is awaiting approved public publishing. Do not deploy this older image. See README.md.')
root=pathlib.Path('/run/relay')
root.mkdir(exist_ok=True)
os.chown('/storage',1000,1000)
os.umask(0o077)
origin=os.environ.get('RELAY_PUBLIC_URL','http://localhost:8080').rstrip('/')
url=urllib.parse.urlsplit(origin)
if url.scheme not in ['http','https'] or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:
    raise SystemExit('RELAY_PUBLIC_URL must be an HTTP(S) origin without credentials or a path')
if (root/'ready').exists():
    if (root/'origin').read_text()!=origin:raise SystemExit('Public URL changed. See docs/operations.md before changing an existing installation.')
    print('Existing installation preserved.');raise SystemExit(0)
password=lambda:secrets.token_hex(32)
db,app,kc,admin,jwt,turn,storage=[password() for _ in range(7)]
def write(name,text):
 p=root/name;p.write_text(text);p.chmod(0o600);os.chown(p,1000,1000)
def env(name,values):write(name,'\n'.join(k+'='+"'"+str(v).replace("'","'\\''")+"'" for k,v in values.items())+'\n')
write('db-password',db)
write('database.sql',f"CREATE USER relay_app WITH PASSWORD '{app}';\nGRANT ALL ON DATABASE relay TO relay_app;\nGRANT ALL ON SCHEMA public TO relay_app;\nCREATE USER keycloak_app WITH PASSWORD '{kc}';\nCREATE DATABASE keycloak OWNER keycloak_app;\n")
env('relay.env',{'DATABASE_URL':f'postgres://relay_app:{app}@db:5432/relay','JWT_SECRET':jwt,'MEDIA_GRANT_SECRET':password(),'KEYCLOAK_ISSUER':origin+'/auth/realms/relay','KEYCLOAK_JWKS_URI':'http://keycloak:8080/auth/realms/relay/protocol/openid-connect/certs','KEYCLOAK_CLIENT_ID':'relay','RELAY_SESSION_ISSUER':origin,'CLIENT_ORIGIN':origin,'TRUST_PROXY':'1','PORT':'3001','RELAY_SELF_HOSTED':'true','S3_ENDPOINT':'http://storage:9000','S3_BUCKET':'relay-media','S3_REGION':'us-east-1','S3_FORCE_PATH_STYLE':'true','S3_CREATE_BUCKET':'true','S3_ACCESS_KEY_ID':'relay','S3_SECRET_ACCESS_KEY':storage,'TURN_URL':os.environ.get('RELAY_TURN_URL','turn:127.0.0.1:3478?transport=udp'),'TURN_USERNAME':'relay','TURN_CREDENTIAL':turn,'PUSH_GATEWAY_URL':'https://push.r3l4y.dev','PUSH_GATEWAY_SERVER_SECRET':secrets.token_urlsafe(32)})
env('keycloak.env',{'KC_DB':'postgres','KC_DB_URL':'jdbc:postgresql://db:5432/keycloak','KC_DB_USERNAME':'keycloak_app','KC_DB_PASSWORD':kc,'KC_BOOTSTRAP_ADMIN_USERNAME':'relay-admin','KC_BOOTSTRAP_ADMIN_PASSWORD':admin,'KC_HOSTNAME':origin+'/auth','KC_HTTP_RELATIVE_PATH':'/auth','KC_HTTP_ENABLED':'true','KC_PROXY_HEADERS':'xforwarded'})
env('storage.env',{'MINIO_ROOT_USER':'relay','MINIO_ROOT_PASSWORD':storage})
realm=json.loads(pathlib.Path('/bootstrap/realm.json').read_text().replace('{{PUBLIC_ORIGIN}}',origin))
realm['sslRequired']='none' if url.scheme=='http' else 'external'
realm['users'][0]['credentials'][0]['value']=admin
realm['users'][0]['username']='relay-admin';realm['users'][0]['email']=os.environ.get('RELAY_ADMIN_EMAIL','admin@example.invalid')
write('relay-realm.json',json.dumps(realm))
write('turn.conf',f'listening-port=3478\nfingerprint\nlt-cred-mech\nrealm=relay\nuser=relay:{turn}\nmin-port=49160\nmax-port=49200\nno-cli\nno-tls\nno-dtls\nno-multicast-peers\nno-loopback-peers\nlog-file=stdout\n')
write('admin.txt',f'URL: {origin}\nUsername: relay-admin\nTemporary password: {admin}\nChange the password at first sign-in.\n')
write('origin',origin)
write('ready','ok\n')
print('Relay initialized. To view your initial credentials: docker compose run --rm init cat /run/relay/admin.txt')
