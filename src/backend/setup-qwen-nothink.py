"""为 qwen3.8-flash 增加一个关闭深度思考的「极速」模型条目。

背景：qwen3.8-flash 是深度思考模型，默认开启 thinking，分类/路由节点会被
80~118 字的思考过程拖慢（实测默认 1.2~1.8s，关闭后 0.5~0.7s）。

链路（已逐段验证）：
  llm_model.config.user_kwargs.extra_body.enable_thinking=false
    -> _get_user_kwargs()                 (llm/domain/llm/llm.py:58)
    -> _get_openai_params() 合并进 params (llm.py:118)
    -> _get_qwen_params() 补 enable_search (llm.py:177)
    -> ChatQwen(**params).extra_body
    -> BaseChatOpenAI._default_params     (langchain_openai)
    -> openai SDK _merge_mappings 拍平为请求体顶层字段
    -> DashScope compatible-mode/v1/chat/completions

幂等：重复执行只更新已有行 config，不新增。
"""
import json
import sys

import pymysql

MODEL_NAME = "qwen3.8-flash"
FAST_MODEL_NAME = "Qwen3.8-Flash 极速"
PROVIDER_ID = 4  # 通义千问MaaS
DESC = "qwen3.8-flash 关闭深度思考，用于意图分类/路由等轻量节点"


def main() -> int:
    conn = pymysql.connect(host="localhost", port=3306, user="root", password="1234",
                           database="bisheng", charset="utf8mb4", autocommit=False)
    cur = conn.cursor(pymysql.cursors.DictCursor)
    try:
        cur.execute("SELECT id,name,model_name,config,online,status FROM llm_model "
                    "WHERE server_id=%s AND model_name=%s", (PROVIDER_ID, MODEL_NAME))
        base = cur.fetchone()
        if not base:
            print("未找到 %s" % MODEL_NAME)
            return 1
        print("基准模型: id=%s name=%s online=%s status=%s"
              % (base["id"], base["name"], base["online"], base["status"]))

        cur.execute("SELECT id,name,config FROM llm_model WHERE name=%s", (FAST_MODEL_NAME,))
        row = cur.fetchone()
        cfg = {"user_kwargs": {"extra_body": {"enable_thinking": False}}}
        if row:
            cur.execute("UPDATE llm_model SET config=%s WHERE id=%s",
                        (json.dumps(cfg, ensure_ascii=False), row["id"]))
            print("已存在，更新 config -> id=%s" % row["id"])
            new_id = row["id"]
        else:
            cur.execute(
                "INSERT INTO llm_model (name, description, model_name, model_type, server_id, "
                "config, online, status, user_id, tenant_id, create_time, update_time) "
                "VALUES (%s,%s,%s,'llm',%s,%s,1,0,1,1,NOW(),NOW())",
                (FAST_MODEL_NAME, DESC, MODEL_NAME, PROVIDER_ID,
                 json.dumps(cfg, ensure_ascii=False)))
            new_id = cur.lastrowid
            print("新增极速模型 -> id=%s" % new_id)

        conn.commit()
        cur.execute("SELECT id,name,model_name,config,online,status FROM llm_model WHERE id=%s",
                    (new_id,))
        print("结果:", json.dumps(cur.fetchone(), ensure_ascii=False, default=str))
        print("\n用法：把工作流 LLM 节点的 model_id 从 %s 改成 %s" % (base["id"], new_id))
        return 0
    except Exception as e:
        conn.rollback()
        print("失败:", e)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
