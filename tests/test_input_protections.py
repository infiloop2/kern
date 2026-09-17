"""Keep the direct-input guide contract aligned with real guard call sites."""
import json
import unittest
from unittest.mock import patch

from host.tools.manifest import ActionSpec, InputProtection, guarded_input, validated_input, protect_inputs
from test_param_guard_coverage import _bundled_manifests, GUARDED_FIELDS
from test_tools import FakeHostAPI


class InputProtectionTests(unittest.TestCase):
    def test_every_direct_input_is_declared_and_guard_classification_matches(self):
        for manifest in _bundled_manifests():
            for action in manifest.actions:
                with self.subTest(tool=manifest.tool_id, action=action.id):
                    if action.approval == "operator":
                        self.assertFalse(action.input_protections)
                        text = json.dumps(action.input_schema).lower()
                        for phrase in ('parameter guard', 'parameter-guarded', 'validated:', 'protection:'):
                            self.assertNotIn(phrase, text)
                        continue
                    self.assertEqual(set(action.input_protections), set(action.input_schema['properties']))
                    for name, protection in action.input_protections.items():
                        guarded = (manifest.tool_id, action.id, name) in GUARDED_FIELDS
                        self.assertEqual(protection.kind == 'parameter_guard', guarded, (manifest.tool_id, action.id, name))

    def test_invalid_and_incomplete_declarations_fail(self):
        for kwargs in ({'kind':'unknown'}, {'kind':'validated'},
                       {'kind':'validated','description':'An ID.','allow_identifiers':True},
                       {'kind':'parameter_guard','description':'Custom guard essay.'},
                       {'kind':'parameter_guard','allow_machine_tokens':1},
                       {'kind':'parameter_guard','allow_longer_text':1},
                       {'kind':'validated','description':'An ID.','allow_longer_text':True},
                       {'kind':'parameter_guard','identifiers_condition':'decimal'},
                       {'kind':'parameter_guard','allow_identifiers':True,'identifiers_condition':'unknown'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                InputProtection(**kwargs)
        schema={'type':'object','properties':{'query':{'type':'string'}},'additionalProperties':False}
        action=ActionSpec('read','Read.','Read public data.',schema,{'type':'object','properties':{},'additionalProperties':False})
        for declarations in ({}, {'read':{}}, {'read':{'wrong':guarded_input()}}, {'wrong':{'query':guarded_input()}}):
            with self.assertRaises(ValueError):
                protect_inputs((action,), declarations)
        attached=protect_inputs((action,), {'read':{'query':guarded_input()}})[0]
        self.assertEqual(attached.input_protections['query'], guarded_input())
        self.assertEqual(attached.input_schema, action.input_schema)
        self.assertFalse(action.input_protections)
        approved=ActionSpec('write','Write.','Write after approval.',schema,approval='operator')
        with self.assertRaises(ValueError):
            protect_inputs((approved,), {'write':{'query':validated_input('Text.')}})

    def test_guard_exceptions_match_real_execution_calls(self):
        from host.tools import gmail, instagram_discovery, vercel_analytics, zoho_mail, upwork, runway
        from host.tools.upwork import validation
        api=FakeHostAPI()
        class GuardReached(Exception):
            pass
        cases=[
            (runway, 'generate_video', 'prompt', lambda: runway._generation_request(api,{'prompt':'hello'},{})),
            (runway, 'edit_video', 'prompt', lambda: runway._edit_request(api,{'prompt':'hello','video_asset_id':'asset'},{})),
            (runway, 'generate_image', 'prompt', lambda: runway._image_request(api,{'prompt':'hello'})),
            (gmail, 'search_messages', 'query', lambda: gmail._gmail_search_query({'query':'hello'},api)),
            (gmail, 'list_drafts', 'query', lambda: gmail._draft_list_parameters({'query':'hello'},api)),
            (gmail, 'list_drafts', 'page_token', lambda: gmail._draft_list_parameters({'page_token':'opaque'},api)),
            (gmail, 'read_message', 'message_id', lambda: gmail._direct_provider_token({'message_id':'123abc'},'message_id',api)),
            (gmail, 'read_thread', 'thread_id', lambda: gmail._direct_provider_token({'thread_id':'123abc'},'thread_id',api)),
            (instagram_discovery, 'get_reels_by_audio', 'cursor', lambda: instagram_discovery._audio_cursor('123456',api)),
            (vercel_analytics, 'list_projects', 'cursor', lambda: vercel_analytics._request('list_projects',{'cursor':'opaque'},api)),
            (zoho_mail, 'search_messages', 'search_key', lambda: zoho_mail._search_messages('access','com',{'search_key':'hello'},api)),
            (zoho_mail, 'create_folder', 'name', lambda: zoho_mail._create_folder('access','com','123',{'name':'Reports'},api)),
        ]
        api.assets.add('asset')
        for module,action,name,invoke in cases:
            declared=module.MANIFEST.action(action).input_protections[name]
            with self.subTest(tool=module.MANIFEST.tool_id,action=action,name=name), patch.object(api.outbound,'guard_request_parameter_string',side_effect=GuardReached) as guard:
                with self.assertRaises(GuardReached): invoke()
                self.assertEqual(declared.identifiers_condition, 'decimal' if module is instagram_discovery else None)
                self.assertEqual(guard.call_args.kwargs.get('allow_identifiers',False),declared.allow_identifiers)
                self.assertEqual(guard.call_args.kwargs.get('allow_machine_tokens',False),declared.allow_machine_tokens)
                self.assertEqual(guard.call_args.kwargs.get('allow_longer_text',False),declared.allow_longer_text)
        # Upwork recursively guards keys too: inspect the call for the value.
        for action in upwork.MANIFEST.actions:
            for name,declared in action.input_protections.items():
                if declared.kind != 'parameter_guard': continue
                with patch.object(api.outbound,'guard_request_parameter_string',side_effect=lambda value,**kw:value) as guard:
                    validation.arguments(json.dumps({name:'sample'}),api)
                    call=next(c for c in guard.call_args_list if c.args==('sample',))
                    self.assertEqual(call.kwargs['allow_identifiers'],declared.allow_identifiers)
                    self.assertEqual(call.kwargs['allow_machine_tokens'],declared.allow_machine_tokens)
        with patch.object(api.outbound,'guard_request_parameter_string',side_effect=lambda value,**kw:value) as guard:
            instagram_discovery._audio_cursor('opaque',api)
            self.assertFalse(guard.call_args.kwargs['allow_identifiers'])
            self.assertTrue(guard.call_args.kwargs['allow_machine_tokens'])
