"""把管理员手工整理的历史听记压缩包转成只有文字的「历史听记包」（.json.gz，约 10MB）。
平台也可以直接上传原压缩包，这个脚本只在压缩包太大、传不上去时用。

用法：python scripts/history_pack.py 均瑶健康文件_全部门.zip 历史听记包.json.gz
生成的包含真实会议内容，不要提交进仓库。
"""
import collections
import gzip
import json
import pathlib
import sys

sys.path.insert(0, pathlib.Path(__file__).resolve().parent.parent.as_posix())
from app import history  # noqa: E402


def main(src, dst):
    data = history.build(src)
    st = data.pop("stats")
    with gzip.open(dst, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"会议 {len(data['items'])} 场（有日期 {st['dated']}，日期不详 {st['no_date']}）；8月14日以后的 {st['after_cutoff']} 场跳过（以 Excel 为准）")
    print("文件夹分布：", collections.Counter(f for r in data["items"] for f in r["folders"]).most_common())


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
