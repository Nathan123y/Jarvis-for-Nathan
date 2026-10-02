"""Product selection, publication checks and draft snapshots are authoritative."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
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

    def test_missing_list_url_is_resolved_by_verified_product_id(self):
        listing={'id':'TID','name':'Tutor Session Organizer','published':True}
        detail=dict(listing,short_url='https://example.gumroad.com/l/tutor')
        with patch.object(sales,'_cli_json',side_effect=[{'products':[listing]},{'product':detail}]) as cli:
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 1',text)
        self.assertEqual(cli.call_args_list[1].args[0],['products','view','TID'])
        self.assertEqual(sales._product({'product':'tutor-sessions'})['url'],detail['short_url'])

    def test_missing_status_is_verified_and_draft_is_not_connected(self):
        listing={'id':'TID','name':'Tutor Session Organizer','short_url':'https://example.gumroad.com/l/tutor'}
        detail=dict(listing,published=False)
        with patch.object(sales,'_cli_json',side_effect=[{'products':[listing]},{'result':{'product':detail}}]):
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 0',text)
        self.assertIn('Not published yet: Tutor Session Organizer',text)
        self.assertEqual(sales._product({'product':'tutor-sessions'})['url'],'')

    def test_manual_name_capitalization_and_spaces_match_without_guessing(self):
        listing={'name':'  TUTOR   session Organizer  ','published':True,'short_url':'https://example.gumroad.com/l/tutor'}
        with patch.object(sales,'_cli_json',return_value={'products':[listing]}) as cli:
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 1',text)
        self.assertEqual(cli.call_count,1)

    def test_normalized_duplicate_names_remain_ambiguous(self):
        records=[{'name':name,'published':True,'short_url':'https://example.gumroad.com/l/'+str(i)}
                 for i,name in enumerate(['Tutor Session Organizer',' TUTOR  session organizer '])]
        with patch.object(sales,'_cli_json',return_value={'products':records}):
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 0',text)
        self.assertIn('Resolve duplicate product names: Tutor Session Organizer',text)
        self.assertEqual(sales._product({'product':'tutor-sessions'})['url'],'')

    def test_changed_wording_reports_observed_names_and_successful_access(self):
        records=[{'name':'My Tutor Planner','published':True,'short_url':'https://example.gumroad.com/l/tutor'}]
        with patch.object(sales,'_cli_json',return_value={'products':records}):
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 0',text)
        self.assertIn('Gumroad returned 1 seller listings successfully',text)
        self.assertIn('No matching catalog name:',text)
        self.assertIn('My Tutor Planner [published]',text)
        self.assertNotIn('API key',text)
        self.assertEqual(sales._product({'product':'tutor-sessions'})['url'],'')

    def test_zero_due_to_drafts_is_explained_and_shown_on_screen(self):
        records=[{'name':'Tutor Session Organizer','published':False}]
        player=MagicMock()
        with patch.object(sales,'_cli_json',return_value={'products':records}):
            text=sales.run({'action':'sync'},player=player)
        self.assertIn('Not published yet: Tutor Session Organizer',text)
        self.assertIn('Tutor Session Organizer [draft]',text)
        player.show_content.assert_called_once_with('GUMROAD CATALOG SYNC',text[:3900])

    def test_failed_detail_lookup_preserves_existing_bindings(self):
        sales._write_links({'cleaning-jobs':{'published':True,'url':'https://example.gumroad.com/l/clean'}})
        before=self.links.read_bytes()
        records=[{'id':'TID','name':'Tutor Session Organizer','published':True}]
        with patch.object(sales,'_cli_json',side_effect=[{'products':records},RuntimeError('Detail lookup unavailable')]):
            text=sales.run({'action':'sync'})
        self.assertIn('Existing links were preserved',text)
        self.assertNotIn('Connected 0',text)
        self.assertEqual(self.links.read_bytes(),before)

    def test_detail_lookup_cannot_bind_a_different_product(self):
        sales._write_links({'cleaning-jobs':{'published':True,'url':'https://example.gumroad.com/l/clean'}})
        before=self.links.read_bytes()
        record={'id':'TID','name':'Tutor Session Organizer','published':True}
        wrong={'id':'OTHER','name':'Tutor Session Organizer','published':True,'short_url':'https://example.gumroad.com/l/other'}
        with patch.object(sales,'_cli_json',side_effect=[{'products':[record]},{'product':wrong}]):
            text=sales.run({'action':'sync'})
        self.assertIn('did not match',text)
        self.assertEqual(self.links.read_bytes(),before)

    def test_malformed_catalog_does_not_replace_links(self):
        sales._write_links({'cleaning-jobs':{'published':True,'url':'https://example.gumroad.com/l/clean'}})
        before=self.links.read_bytes()
        with patch.object(sales,'_cli_json',return_value={'products':[None]}):
            text=sales.run({'action':'sync'})
        self.assertIn('unexpected catalog',text)
        self.assertEqual(self.links.read_bytes(),before)

    def test_conflicting_publication_flags_require_verification(self):
        record={'name':'Tutor Session Organizer','published':True,'is_published':False,
                'short_url':'https://example.gumroad.com/l/tutor'}
        with patch.object(sales,'_cli_json',return_value={'products':[record]}):
            text=sales.run({'action':'sync'})
        self.assertIn('Connected 0',text)
        self.assertIn('published status missing',text)
        self.assertEqual(sales._product({'product':'tutor-sessions'})['url'],'')

if __name__=='__main__':unittest.main()
