"""Fault injection for raw audit and semantic gates, never real accuracy data."""
import copy
import unittest
from types import SimpleNamespace

from collector.linux.wps_acceptance import WpsTestApplication, SemanticFailure, BASE
from scripts.linux_raw_window_report import reconstruct


def fixture():
    phases=[]; records=[]
    for i,(phase,index,pages) in enumerate((("baseline",0,2),("baseline",1,3),("action",0,7),("post",0,4))):
        started=100+i*100
        phases.append({"phase":phase,"index":index,"processes":[{"pid":17,"sample_start_ns":started,"sample_end_ns":started+30}]})
        records.append({"schema":"harmony.ops.raw-window.v1","feature_schema":"wps.vma.feature.v1",
                        "phase":phase,"index":index,"pid":17,"start_ticks":111,"page_size":4096,
                        "started_ns":started+5,"finished_ns":started+20,
                        "vmas":[{"pathname":"/synthetic/library.so","segment":"filepage_code","perms":"r-xp",
                                 "referenced_bytes":pages*4096,"referenced_pages":pages}]})
    return records,phases


class RawWindowAuditTests(unittest.TestCase):
    def test_complete_replay_uses_the_frozen_formula(self):
        records,phases=fixture();vector,seq=reconstruct(records,phases,[{"pid":17,"start_ticks":111}])
        self.assertEqual(len(vector),2048)
        self.assertEqual(sum(v!=0 for v in vector),1)
        self.assertAlmostEqual(sum(v*v for v in vector),1)
        self.assertEqual(seq["aggregated_features"][0]["aggregated_excess_pages"],4.5)

    def test_partial_or_duplicate_processes_are_rejected(self):
        records,phases=fixture()
        with self.assertRaisesRegex(ValueError,"partial"):reconstruct(records[:-1],phases)
        with self.assertRaisesRegex(ValueError,"repeated"):reconstruct(records+[records[0]],phases)

    def test_pid_reuse_and_outside_window_are_rejected(self):
        records,phases=fixture();bad=copy.deepcopy(records);bad[-1]["start_ticks"]=112
        with self.assertRaisesRegex(ValueError,"identity"):reconstruct(bad,phases)
        bad=copy.deepcopy(records);bad[-1]["finished_ns"]=100000
        with self.assertRaisesRegex(ValueError,"interval"):reconstruct(bad,phases)

    def test_invalid_pages_and_unfrozen_window_are_rejected(self):
        records,phases=fixture();bad=copy.deepcopy(records);bad[0]["vmas"][0]["referenced_pages"]=8
        with self.assertRaisesRegex(ValueError,"VMA"):reconstruct(bad,phases)
        with self.assertRaisesRegex(ValueError,"phases"):reconstruct(records,phases[:-1])


class SemanticGateTests(unittest.TestCase):
    def application(self,font):
        driver=WpsTestApplication.__new__(WpsTestApplication)
        driver.focus=lambda:"synthetic-window"
        document=SimpleNamespace(Content=SimpleNamespace(Text=BASE+"\r"))
        selection=SimpleNamespace(Start=0,End=len(BASE),Font=font,Text=BASE)
        driver.app=SimpleNamespace(ActiveDocument=document,Selection=selection,Documents=SimpleNamespace(Count=2))
        return driver

    def test_unformatted_text_cannot_pass_bold_verification(self):
        driver=self.application(SimpleNamespace(Bold=0))
        with self.assertRaises(SemanticFailure) as context:
            driver.verify({"label":"FORMAT_BOLD","before":BASE,"selection_before":[0,len(BASE)]})
        self.assertFalse(context.exception.evidence["passed"])
        self.assertEqual(context.exception.evidence["format_value"],0)

    def test_existing_text_without_found_selection_is_not_a_find_success(self):
        driver=self.application(SimpleNamespace());driver.app.Selection.Text=""
        with self.assertRaises(SemanticFailure):driver.verify({"label":"FIND_TEXT","before":BASE})


if __name__=="__main__":unittest.main()
