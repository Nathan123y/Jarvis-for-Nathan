"""Product selection, publication checks and draft snapshots are authoritative."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from plugins import product_sales as sales

class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.links=Path(self.directory.name)/'catalog_links.json'
        self.state=Path(self.directory.name)/'state.json'
        self.patches=[patch.object(sales,'_LINKS_FILE',self.links),patch.object(sales,'_STATE_FILE',self.state)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in self.patches:p.stop()
        self.directory.cleanup()
    def test_sixteen_distinct_products_and_unpublished_pitch_gate(self):
        self.assertEqual(len(sales._catalog()),16)
        self.assertEqual(len({p['id'] for p in sales._catalog()}),16)
        self.assertIn('100 prepared rows',sales.run({'action':'brief','product':'tutor-sessions'}))
        text=sales.run({'action':'campaign','product':'tutor-sessions'})
        self.assertIn('no verified published',text)
        self.assertFalse(self.state.exists())
        self.assertIn('Choose one exact',sales.run({'action':'brief','product':'tutor'}))
    def test_sync_requires_published_exact_name_and_unique_match(self):
        p=sales._product({'product':'tutor-sessions'})
        records=[{'name':p['name'],'published':False,'short_url':'https://example.gumroad.com/l/tutor'},
                 {'name':'Cleaning Business Job Organizer','published':True,'short_url':'https://example.gumroad.com/l/clean'},
                 {'name':'Pet Care Booking Organizer','published':True,'short_url':'https://evil.example/l/pet'},
                 {'name':'Music Lesson Studio Organizer','published':True,'short_url':'https://example.gumroad.com/l/m1'},
                 {'name':'Music Lesson Studio Organizer','published':True,'short_url':'https://example.gumroad.com/l/m2'}]
        with patch.object(sales,'_cli_json',return_value={'products':records}):
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 1',text)
        self.assertIn('duplicate product names',text)
        self.assertEqual(sales._product({'product':'tutor-sessions'})['url'],'')
        self.assertEqual(sales._product({'product':'cleaning-jobs'})['url'],'https://example.gumroad.com/l/clean')
        self.assertEqual(self.links.stat().st_mode&0o777,0o600)
    def test_draft_retains_chosen_product_when_links_change(self):
        sales._write_links({'tutor-sessions':{'published':True,'url':'https://example.gumroad.com/l/tutor'}})
        sales.run({'action':'add_lead','name':'Example Tutor','email':'tutor@example.com','context':'Tutoring business','relationship':'Known to the user'})
        result=sales.run({'action':'draft','product':'Tutor Session Organizer','email':'tutor@example.com'})
        self.assertIn('Tutor Session Organizer',result)
        draft=next(iter(sales._load()['drafts'].values()))
        self.assertEqual(draft['product_id'],'tutor-sessions')
        sales._write_links({'tutor-sessions':{'published':True,'url':'https://example.gumroad.com/l/new'}})
        shown=sales.run({'action':'show','draft_id':draft['id'],'product':'cleaning-jobs'})
        self.assertIn('/l/tutor?',shown)
        self.assertNotIn('/l/new',shown)
        self.assertNotIn('Soccer Coach',shown)
    def test_cross_product_switch_does_not_reset_opt_out(self):
        sales._write_links({'tutor-sessions':{'published':True,'url':'https://example.gumroad.com/l/tutor'}})
        sales.run({'action':'add_lead','name':'Example Tutor','email':'tutor@example.com','context':'Tutoring','relationship':'Known'})
        sales.run({'action':'record','email':'tutor@example.com','outcome':'do_not_contact'})
        text=sales.run({'action':'draft','product':'tutor-sessions','email':'tutor@example.com'})
        self.assertIn('opted out',text)
        self.assertFalse(sales._load()['drafts'])
    def test_sales_uses_selected_product(self):
        sales._write_links({'tutor-sessions':{'published':True,'url':'https://example.gumroad.com/l/tutor'}})
        with patch.object(sales,'_cli_json',side_effect=[{'product':{'id':'TID'}},{'sales':[]}]) as cli:
            text=sales.run({'action':'sales','product':'tutor-sessions'})
        self.assertEqual(cli.call_args_list[0].args[0],['products','view','tutor'])
        self.assertIn('TID',cli.call_args_list[1].args[0])
        self.assertIn('0 reported orders',text)

if __name__=='__main__':unittest.main()
