"""Optional local function profiler; invokes the existing writer and all its checks."""
import argparse
import cProfile
import json
from pathlib import Path
import pstats
from types import SimpleNamespace

import pdf_reader


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','manifest','output','font-dir','profile-dir'):
        parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    directory=Path(args.profile_dir);directory.mkdir(parents=True,exist_ok=True)
    profiler=cProfile.Profile()
    status='failed'
    try:
        profiler.runcall(pdf_reader.build,SimpleNamespace(source=args.source,
            manifest=args.manifest,output=args.output,font_dir=args.font_dir))
        status='completed'
    finally:
        profiler.dump_stats(str(directory/'build.prof'))
        stats=pstats.Stats(profiler)
        functions=[]
        for (filename,line,function),(primitive,calls,self_seconds,total_seconds,callers) in stats.stats.items():
            functions.append({'file':Path(filename).name,'line':line,'function':function,
                'calls':calls,'self_seconds':round(self_seconds,6),'cumulative_seconds':round(total_seconds,6)})
        functions.sort(key=lambda row:row['cumulative_seconds'],reverse=True)
        summary={'status':status,'profiling_overhead_included':True,
            'total_seconds':stats.total_tt,'top_functions':functions[:30]}
        (directory/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),'utf-8')
        print(json.dumps({'profile_summary':str((directory/'summary.json').resolve()),'status':status}))


if __name__=='__main__':main()
