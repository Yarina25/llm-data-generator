from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from faker import Faker
import json
import re
from openai import OpenAI, RateLimitError, APIError

app = FastAPI(title="数据构造智能体")
fake = Faker("zh_CN")
client = OpenAI(base_url="https://api.agnes-ai.cn/v1", api_key="sk-B8kQEgN9GXCD1GQljGPkGqyNmGkylvX0Tw0BBuJXudNGM5ba")

class UserQuery(BaseModel):
    prompt: str

def _load_json(raw):
    """容错解析模型输出：去皮 ```json 围栏、去前后解释文字，再取第一个完整对象"""
    if not raw or not raw.strip():
        raise ValueError("模型返回为空，无法解析Schema")
    s = re.sub(r"^```[a-zA-Z]*\s*", "", raw.strip())
    s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            raise ValueError(f"模型未按JSON返回，原始内容前200字符：{s[:200]}")
        return json.loads(m.group(0))


# 1. LLM：自然语言转数据Schema
def nl2schema(prompt: str):
    sys_prompt = """
你是数据规则解析器。把用户自然语言转换成 JSON Schema，描述要生成的数据。

严格按下面的结构输出，键名不得增删改名：
{
  "entity": "实体名（中文字符串）",
  "count": 数量（整数）,
  "relation": "关联关系描述（字符串，没有就给空字符串）",
  "fields": [
    {
      "name": "字段名",
      "type": "string 或 integer 或 number",
      "enum": ["可选值1", "可选值2"],
      "min": 最小值,
      "max": 最大值,
      "unique": true,
      "value": "固定值"
    }
  ]
}

硬性规则：
1. enum / min / max / unique / value 一律平铺在字段对象的第一层，禁止嵌套到 "constraints" 之类的子对象里。
2. 用户明确提到的取值集合、上下限、唯一性、固定值，必须逐条落到对应字段上，一条都不能漏；用不到的可选项直接省略该键。
3. 用户没提到的约束不要臆造。
4. 年龄、数量这类整数字段用 "integer"；薪资、金额、价格用 "number"。
5. 只输出 JSON 本体，不要解释、不要 Markdown 代码块。
"""
    resp = client.chat.completions.create(
        model="agnes-2.5-flash",
        messages=[{"role":"system","content":sys_prompt},{"role":"user","content":prompt}],
        temperature=0,
    )
    return _load_json(resp.choices[0].message.content)

# 2. 根据schema生成数据
def _flatten_field(f):
    """兼容模型把约束嵌套在 constraints 里、或直接平铺在字段上两种写法"""
    flat = dict(f)
    c = flat.pop("constraints", None)
    if isinstance(c, dict):
        for k, v in c.items():
            flat.setdefault(k, v)
    return flat


def generate_by_schema(schema):
    res = []
    for _ in range(schema["count"]):
        item = {}
        for raw in schema["fields"]:
            f = _flatten_field(raw)
            fname = f["name"]
            ftype = str(f.get("type", "")).lower()
            if "enum" in f:
                item[fname] = fake.random_element(f["enum"])
            elif "value" in f:
                item[fname] = f["value"]
            elif ftype in ("number", "integer", "int", "float"):
                item[fname] = fake.random_int(min=f.get("min", 1), max=f.get("max", 9999))
            elif ftype == "string":
                item[fname] = fake.company() if "name" in fname else fake.name()
        res.append(item)
    return res

@app.post("/api/generate_data")
def generate(q: UserQuery):
    try:
        schema = nl2schema(q.prompt)
    except RateLimitError:
        # 上游免费额度限流：以前这里会变成裸 500，现在明确告诉调用方是限流
        raise HTTPException(
            status_code=429,
            detail="上游模型速率受限（免费额度），请稍后重试；连续调用请加间隔或升级配额",
        )
    except APIError as e:
        raise HTTPException(status_code=502, detail=f"上游模型调用失败：{e}")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"模型返回无法解析为 Schema：{e}")

    data = generate_by_schema(schema)
    return {"schema": schema, "data": data}
