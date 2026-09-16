import json,pathlib,sys,unittest
ROOT=pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'scripts'))
from deploy import safe_origin,configure
from create_cost_report import walk_resources
from report import write_report
import tempfile,argparse
class ContractTests(unittest.TestCase):
 def test_origin_rejects_credentials_paths_and_http(self):
  for value in ['http://example.com','https://x:y@example.com','https://example.com/path','https://example.com?token=secret']:
   with self.assertRaises(ValueError):safe_origin(value)
  self.assertEqual(safe_origin('https://chat.example.com/'),'https://chat.example.com')
 def test_uncertified_large_tier_rejected_before_cloud_changes(self):
  args=argparse.Namespace(non_interactive=True,home_lab=False,name='example',region='eu-central-1',users='10k',admin_email='a@example.com',public_url='',zone_id='')
  with self.assertRaisesRegex(ValueError,'not certified'):configure(args,{'Account':'123456789012'},{'version':'1.0.0','scaleReady':False})
 def test_nested_stack_addresses_keep_pricing_classification(self):
  root={'child_modules':[{'resources':[{'mode':'managed','address':'module.relay.aws_s3_bucket.relay["media"]','type':'aws_s3_bucket','values':{}}]}]}
  resources=list(walk_resources(root));self.assertEqual(resources[0].address,'aws_s3_bucket.relay["media"]')
 def test_public_locks_are_immutable_and_contain_no_private_registry(self):
  for file in ['release.lock.json','platform-images.lock.json']:
   data=json.loads((ROOT/file).read_text())
   for image in data['images'].values():
    self.assertTrue(image['source'].startswith('public.ecr.aws/m0z0w6q1/'))
    self.assertRegex(image['digest'],r'^sha256:[a-f0-9]{64}$')
 def test_html_report_escapes_values_and_omits_raw_plan_values(self):
  config={k:'<script>bad</script>' for k in ['name','account_id','region','users','home_lab','public_origin']}
  pricing={'summary':dict(monthlyTotal='1',monthlyFixed='1',monthlyUsage='0',annualTotal='12'),'components':[],'usageModel':{},'exclusions':[],'priceSource':{'queriedAt':'today'}}
  with tempfile.TemporaryDirectory() as d:
   file=pathlib.Path(d)/'report.html';write_report(file,config,{'resource_changes':[],'planned_values':{'password':'DO_NOT_RENDER'}},pricing,'a'*64);html=file.read_text()
   self.assertNotIn('<script>bad</script>',html);self.assertNotIn('DO_NOT_RENDER',html);self.assertIn('&lt;script&gt;',html)
if __name__=='__main__':unittest.main()
