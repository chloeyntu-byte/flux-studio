"""Rebuild fictional technology and procedural winding references.

No source file from a technology kit, layout export or simulation is read.
"""
from pathlib import Path
import json
import hashlib

def main():
    r = Path(__file__).resolve().parents[1]
    # All numeric rules below are invented demonstration values.
    for index,name in enumerate(('generic_a','generic_b','generic_c','generic_d')):
     grid=(.01,.02,.025,.005)[index]
     names=['M%d'%n for n in range(1,10)]+['AP']
     tech={'schema':'flux.technology.v1','generic_example':True,'description':'Fictional demo only; not a manufacturing technology.',
     'library':name,'technology':name,'library_path':'/path/to/design_library','grid':grid,'manufacturing_grid':grid,'dbu_per_uu':1000,
     'order_verified':True,'layers':[{'name':n,'number':200+i*7,'order':i+1,'order_verified':True,'min_width':.3,'min_spacing':.25,'min_area':.1} for i,n in enumerate(names)],
     'vias':[{'name':'V_%s_%s'%(lo,hi),'lower':lo,'upper':hi,'cut_layer':'CUT%d'%i,'cut_size':[.6,.6],'cut_spacing':[.4,.4],'lower_enclosure':[.2,.2],'upper_enclosure':[.2,.2]} for i,(lo,hi) in enumerate(zip(names,names[1:]),1)]}
     (r/'tests/fixtures'/(name+'.json')).write_text(json.dumps(tech,indent=2)+'\n')
     if index==0:(r/'examples/generic_technology.json').write_text(json.dumps(tech,indent=2)+'\n')
    # Procedural drawing references, never geometry imported from a technology.
    reference_dir=r/'snpfit/visual_references';reference_dir.mkdir(exist_ok=True)
    for stack in ('generic_a','generic_b','generic_c','generic_d'):
     for family in ('inductor','tapped_inductor','transformer'):
      identity=stack+'/'+family
      paths=[{'id':'L','path':[[-.2,-.5],[-.4,-.3],[-.4,.3],[.4,.3],[.4,-.3],[.2,-.5]],'positive_file_port':1,'negative_file_port':2}]
      if family=='transformer':paths=[dict(paths[0],id='P'),{'id':'S','path':[[-.1,-.4],[-.3,-.2],[-.3,.2],[.3,.2],[.3,-.2],[.1,-.4]],'positive_file_port':3,'negative_file_port':4}]
      pins=[]
      for w in paths:
       for port,point in ((w['positive_file_port'],w['path'][0]),(w['negative_file_port'],w['path'][-1])):
        pins.append({'name':('L' if w['id']=='L' else w['id'])+str(1 if port==w['positive_file_port'] else 2),'kind':'signal','file_port':port,'point':point,'point_um':[v*100 for v in point]})
      taps=[]
      if family=='tapped_inductor':
       taps=[{'name':'CTAP','winding':'L','attachment_fraction':.5,'attachment':[0,.3],'path':[[0,.3],[.1,.5]]}]
       pins.append({'name':'CTAP','kind':'tap','file_port':3,'point':[.1,.5],'point_um':[10,50]})
      for k in range(2):pins.append({'name':'GND%d'%(k+1),'kind':'ground','file_port':None,'point':[.6,k*.2],'point_um':[60,k*20]})
      digest=hashlib.sha256(json.dumps(paths,sort_keys=True).encode()).hexdigest()
      profile={'id':identity,'label':identity+' (synthetic)','pdk':stack,'family':family,'normalization':{'origin_um':[0,0],'scale_um':100},'windings':paths,'pins':pins,'tap_paths':taps,'source_snp_sha256':[hashlib.sha256(('synthetic fixture '+identity).encode()).hexdigest()], 'provenance':{'source':'procedurally generated illustration; no imported layout or simulation','geometry_sha256':digest,'generator_sha256':digest}}
      (reference_dir/(identity.replace('/','__')+'.json')).write_text(json.dumps(profile,indent=2)+'\n')

    for p in list((r/'tests/fixtures').glob('*.json'))+[r/'examples/generic_technology.json']:
     tech=json.loads(p.read_text())
     for layer in tech['layers']:
      layer['max_width']=12 if layer['name']!='AP' else 24
      layer['min_spacing']=1.5 if layer['name'] not in ('M1','M2') else .25
     for i,v in enumerate(tech['vias']):
      if i==0:size,space,enc=.4,.3,.15
      elif i==8:size,space,enc=3.6,1.2,1.1
      else:size,space,enc=.8,.4,.3
      v.update(cut_size=[size,size],cut_spacing=[space,space],lower_enclosure=[enc,enc],upper_enclosure=[enc,enc])
     p.write_text(json.dumps(tech,indent=2)+'\n')

if __name__ == '__main__':
    main()
