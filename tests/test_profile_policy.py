"""4C1 registry/metadata. Không đọc config thật hoặc gọi API."""
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from hma.profile_policy import ProfilePolicy, BASELINE_POLICY, policy_for_profile
from hma.memory_context import MemoryExecutionContext
from hma.runtime_binding import context_metadata, validate_entry_context
from hma.tools import ToolSettings
from hma.runtime_lock import RunnerError


class ProfilePolicyTests(unittest.TestCase):
    def test_protected_enforces_ownership(self):
        self.assertTrue(policy_for_profile('protected').enforce_ticket_ownership)

    def test_vulnerable_observes_ownership(self):
        self.assertFalse(policy_for_profile('vulnerable').enforce_ticket_ownership)

    def test_legacy_baseline_is_strict_not_a_live_choice(self):
        self.assertIs(policy_for_profile(),BASELINE_POLICY)
        self.assertIsNone(BASELINE_POLICY.name)
        self.assertTrue(BASELINE_POLICY.enforce_ticket_ownership)
        self.assertEqual(BASELINE_POLICY.stage,'binding_only')

    def test_invalid_names_and_direct_boolean_flags_are_rejected(self):
        for name in ('baseline','auto','Protected',False,0,[],{}):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    ProfilePolicy(name)
        with self.assertRaises(TypeError):
            ProfilePolicy('protected',enforce_ticket_ownership=False)

    def test_profile_policy_is_immutable(self):
        policy=policy_for_profile('protected')
        with self.assertRaises(FrozenInstanceError):
            policy.name='vulnerable'

    def test_factory_keeps_all_other_settings(self):
        cfg=SimpleNamespace(sender_classes={'customer.test':'client'},handoff={'client':'alice@corp.test'},
                            agent=SimpleNamespace(kb_top_k=3,max_replies_per_recipient_per_day=20),profile='protected')
        settings=ToolSettings.from_config(cfg,profile='vulnerable')
        self.assertEqual((settings.kb_top_k,settings.reply_daily_cap),(3,20))
        self.assertEqual(settings.sender_classes,cfg.sender_classes)
        self.assertEqual(settings.handoff,cfg.handoff)
        self.assertEqual(settings.profile_policy.name,'vulnerable')

    def test_metadata_labels_alone_do_not_select_active_policy(self):
        memory=MemoryExecutionContext('fixture','vulnerable')
        self.assertEqual(context_metadata(memory)['profile_policy_stage'],'binding_only')
        self.assertEqual(context_metadata(memory,profile_policy=BASELINE_POLICY)['profile_policy_stage'],'binding_only')

    def test_active_metadata_uses_same_selected_name_and_stage(self):
        for name in ('protected','vulnerable'):
            policy=policy_for_profile(name)
            memory=MemoryExecutionContext('fixture',name)
            data=context_metadata(memory,profile_policy=policy)
            self.assertEqual(data['profile'],name)
            self.assertEqual(data['profile_policy_stage'],policy.stage)
            self.assertIn('ownership',policy.stage)

    def test_metadata_refuses_mismatch_or_missing_context(self):
        with self.assertRaises(ValueError):
            context_metadata(MemoryExecutionContext('fixture','protected'),profile_policy=policy_for_profile('vulnerable'))
        with self.assertRaises(ValueError):
            context_metadata(None,profile_policy=policy_for_profile('protected'))

    def test_active_report_refuses_wrong_stage(self):
        policy=policy_for_profile('protected')
        memory=MemoryExecutionContext('fixture','protected')
        data=context_metadata(memory,profile_policy=policy)
        validate_entry_context([data],memory,profile_policy=policy)
        with self.assertRaises(RunnerError):
            validate_entry_context([dict(data,profile_policy_stage='binding_only')],memory,profile_policy=policy)


if __name__=='__main__':
    unittest.main()
