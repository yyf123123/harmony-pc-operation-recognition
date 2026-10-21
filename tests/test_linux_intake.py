"""Received evidence must not bypass archive, receipt or numerical checks."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import warnings
import zipfile

from collector.linux.intake import EvidenceZip, VECTOR_HEADER, relative_name, strict_json, vector_row, verify_journal


def write_archive(path, files, hashes=None):
    manifest=hashes or {name:hashlib.sha256(data).hexdigest() for name,data in files.items()}
    with zipfile.ZipFile(path,"w") as z:
        for name,data in files.items():z.writestr(name,data)
        z.writestr("SHA256SUMS.json",json.dumps(manifest))


class PilotIntakeTests(unittest.TestCase):
    def test_manifest_rejects_changed_content_and_uncovered_extra_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"input.zip"
            files={"data.json":b'{"source":"synthetic-unit-test"}'}
            write_archive(path,files)
            z=EvidenceZip(path)
            try:self.assertEqual(z.verify_manifest(),1)
            finally:z.close()
            write_archive(path,{"data.json":b'changed'}, {"data.json":hashlib.sha256(files["data.json"]).hexdigest()})
            z=EvidenceZip(path)
            try:
                with self.assertRaisesRegex(ValueError,"SHA-256"):z.verify_manifest()
            finally:z.close()
            write_archive(path,{**files,"extra.py":b'not executed'}, {"data.json":hashlib.sha256(files["data.json"]).hexdigest()})
            z=EvidenceZip(path)
            try:
                with self.assertRaisesRegex(ValueError,"exactly all"):z.verify_manifest()
            finally:z.close()

    def test_paths_symlinks_duplicates_and_nonfinite_json_are_rejected(self):
        for name in ("../data","/absolute","a/../b","a\\b","a//b"):
            with self.assertRaises(ValueError):relative_name(name)
        with self.assertRaises(ValueError):strict_json('{"x":NaN}')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"input.zip"
            with zipfile.ZipFile(path,"w") as z:
                entry=zipfile.ZipInfo("link");entry.create_system=3;entry.external_attr=0o120777<<16
                z.writestr(entry,"/outside")
            with self.assertRaisesRegex(ValueError,"symlink"):EvidenceZip(path)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore",UserWarning)
                with zipfile.ZipFile(path,"w") as z:z.writestr("same","first");z.writestr("same","second")
            with self.assertRaisesRegex(ValueError,"duplicate"):EvidenceZip(path)

    def test_valid_outer_hash_cannot_cover_a_wrong_receipt_or_incomplete_journal(self):
        base="raw/synthetic-unit-test/"
        name="attempts/unit/try-001/sample-000/derived/sample.json"
        sample=json.dumps({"operation_id":"wps.writer.write_text"}).encode()
        unit={"unit_id":"unit","stage":"formal","operations":["wps.writer.write_text"]}
        from collector.linux.batch import digest
        config={"fixture":"unit-test-only"}
        binding={"config_sha256":digest(config),"fixture_mode":False}
        receipt={"event":"passed","unit_id":"unit","attempt":1,"stage":"formal","samples":[name],"files":{name:"0"*64}}
        journal=json.dumps({"event":"started","unit_id":"unit","attempt":1})+"\n"+json.dumps(receipt)+"\n"
        files={base+"config.json":json.dumps(config).encode(),base+"binding.json":json.dumps(binding).encode(),
               base+"schedule.json":json.dumps([unit]).encode(),base+"journal.jsonl":journal.encode(),base+name:sample}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"input.zip"
            write_archive(path,files)
            z=EvidenceZip(path)
            try:
                z.verify_manifest()
                with self.assertRaisesRegex(ValueError,"receipt hash"):verify_journal(z,"synthetic-unit-test")
            finally:z.close()
            files[base+"journal.jsonl"]=journal.rstrip().encode()
            write_archive(path,files)
            z=EvidenceZip(path)
            try:
                z.verify_manifest()
                with self.assertRaisesRegex(ValueError,"truncated"):verify_journal(z,"synthetic-unit-test")
            finally:z.close()

    def test_vector_identity_zero_and_nonfinite_values_are_rejected(self):
        row=dict(zip(VECTOR_HEADER,["fixture",1]+[0]*2047))
        self.assertEqual(len(vector_row([row],"fixture")),2048)
        with self.assertRaisesRegex(ValueError,"identity"):vector_row([row],"different")
        for bad in (0,float("nan"),float("inf")):
            with self.assertRaises(ValueError):vector_row([{**row,"v0000":bad}],"fixture")


if __name__=="__main__":unittest.main()
