"""用真的向量模型检查一遍（CI 里跑，需要联网下载约 100MB 模型）：
下载 → 加载 → 分词 → 意思相近的句子相似度要明显更高。"""
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="tingji-embed-")
sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parent.parent.as_posix())
from app import embed  # noqa: E402

embed._download()
embed._load()
ids = embed._tok.encode("盒马订单增长2900箱，PET产线产能紧张")
assert ids[0] == embed._tok.cls and ids[-1] == embed._tok.sep and embed._tok.unk not in ids[1:-1], ids
q = embed.embed(["春节前生产产能不够怎么办"], query=True)[0]
docs = embed.embed(["一月份因盒马订单新增，原味产品线产能紧张，协调衢州与宜昌工厂增加PET产量",
                    "候选人介绍了自己在快消行业十六年的工作经历",
                    "家长会讲解中考政策和各学科学习方法"])
sims = docs @ q
print("相似度：", [round(float(s), 3) for s in sims])
assert sims[0] > sims[1] and sims[0] > sims[2], "向量模型结果不对"
print("向量模型正常")
