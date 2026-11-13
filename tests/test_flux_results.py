import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import flux_results as results


class ResultServiceTests(unittest.TestCase):
    def setup_row(self,root,frequency=(1e9,20e9)):
        snp=root/'response.s4p';snp.write_text('fixture for transport contract')
        workspace=root/'workspace';workspace.mkdir();mapping=workspace/'alternate.lib';mapping.write_text('')
        lib=workspace/'DesignLib';lib.mkdir()
        identity=dict(library='DesignLib',cell='SelectedCandidate',technology='AttachedTech',workspace=str(workspace),cds_lib=str(mapping),library_path=str(lib))
        signals=['P1','S1','P2','S2'];terminals=[]
        for winding in ('P','S'):
            for endpoint,suffix in [('positive','1'),('negative','2')]:terminals.append(dict(name=winding+suffix,winding=winding,endpoint=endpoint,kind='signal'))
        network=dict(ports=dict(signals=signals,terminals=terminals,grounds=[]),frequencies_hz=list(frequency),snp_sha256=hashlib.sha256(snp.read_bytes()).hexdigest())
        row=dict(id='candidate',status='completed',snp_path=str(snp),source=identity,design=dict(model='differential_inductor',parameters={},library='DesignLib',cell='SelectedCandidate'),network=network,metrics=[dict(frequency_hz=f,Lp=.3,Qp=20) for f in frequency])
        source=dict(identity=dict(identity,cell='OtherCell'),runtime=dict(workspace=str(workspace),cds_lib=str(mapping),environment={},user='root'))
        return row,source

    def test_request_maps_actual_interleaved_signal_order(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);row,source=self.setup_row(root)
            with patch('result_data.hydrate_row',side_effect=copy.deepcopy):
                selected,request=results._request(row,source,root/'out')
            self.assertEqual(request['port_order'],[1,3,2,4])
            self.assertEqual(request['cell'],'SelectedCandidate')
            self.assertTrue(request['overwrite'])
            self.assertEqual(request['l_pct'],5);self.assertEqual(request['q_pct'],20)
            self.assertEqual(request['fmin_ghz'],1);self.assertEqual(request['fmax_ghz'],20)
            self.assertEqual(row['source']['cell'],'SelectedCandidate')

    def test_single_point_refuses_fit_but_allows_external_view(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);row,source=self.setup_row(root,[20e9])
            with patch('result_data.hydrate_row',side_effect=copy.deepcopy):
                with self.assertRaisesRegex(ValueError,'at least two actual simulated frequencies'):results._request(row,source,root/'out')
                selected,request=results._request(row,source,root/'out',require_fit=False)
                self.assertEqual(request['source_path'],row['snp_path'])

    def test_durable_network_requires_no_old_job_or_layout(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);row,source=self.setup_row(root);row['job_dir']='/missing/removed/transient'
            with patch('result_data.hydrate_row',side_effect=copy.deepcopy):
                selected,request=results._request(row,source,root/'out')
            self.assertEqual(request['source_sha256'],row['network']['snp_sha256'])
            self.assertFalse((Path(row['source']['library_path'])/'SelectedCandidate').exists())

    def test_missing_or_corrupt_source_never_uses_cached_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);row,source=self.setup_row(root)
            with patch('result_data.hydrate_row',return_value=dict(row,data_error='hash changed',metrics=[])):
                with self.assertRaisesRegex(ValueError,'hash changed'):results._selected(row,source)

    def test_sibling_discovery_supports_spaces_and_explicit_override(self):
        with tempfile.TemporaryDirectory(prefix='Flux API space ') as d:
            root=Path(d);flux=root/'flux tool';flux.mkdir();snp=root/'snpfit_studio_20261101';(snp/'snpfit').mkdir(parents=True)
            (snp/'launch').touch();(snp/'snpfit/integration_api.py').touch()
            with patch.object(results,'ROOT',flux),patch.dict(os.environ,{},clear=True):self.assertEqual(results.discover_snpfit(),snp)
            other=root/'custom fit';(other/'snpfit').mkdir(parents=True);(other/'launch').touch();(other/'snpfit/integration_api.py').touch()
            with patch.object(results,'ROOT',flux),patch.dict(os.environ,{'TFGEN_SNPFIT_ROOT':str(other)}):self.assertEqual(results.discover_snpfit(),other)

    def test_private_request_does_not_overwrite_and_has_private_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'request.json';results._private_json(p,{'data':1})
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            with self.assertRaises(FileExistsError):results._private_json(p,{'data':2})
            self.assertEqual(json.loads(p.read_text()),{'data':1})
