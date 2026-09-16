# -*- coding: utf-8 -*-
import json
import os
import time
import asyncio
import inspect
import random
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.message_components import Plain

from .scales import (
    SDS_DATA, SAS_DATA, SCL90_DATA, PHQ9_DATA, GAD7_DATA, 
    PCL5_DATA, SWLS_DATA, EPQN_DATA, SAD_DATA, UCLA_DATA, 
    SES_DATA, LES_DATA
)

DISCLAIMER = (
    "\n\n💡 温馨提示：本报告由心晴助手生成，仅供心理健康筛查参考，不具备临床诊断效力。"
    "如觉不适，请务必咨询专业心理医生或拨打危机干预热线。"
)

SCALE_META = {
    "1": {"data": SDS_DATA, "desc": "评估近期抑郁情绪的程度", "scoring": "1-4分评分，含反向计分"},
    "2": {"data": SAS_DATA, "desc": "评估近期焦虑情绪的程度", "scoring": "1-4分评分，含反向计分"},
    "3": {"data": PHQ9_DATA, "desc": "国际通用抑郁快速筛查", "scoring": "0-3分评分"},
    "4": {"data": GAD7_DATA, "desc": "国际通用焦虑快速筛查", "scoring": "0-3分评分"},
    "5": {"data": PCL5_DATA, "desc": "创伤后应激反应严重度评估", "scoring": "0-4分评分"},
    "6": {"data": SWLS_DATA, "desc": "主观生活满意度认知评估", "scoring": "1-7分评分"},
    "7": {"data": EPQN_DATA, "desc": "情绪稳定性与神经质倾向", "scoring": "0-1分评分"},
    "8": {"data": SCL90_DATA, "desc": "十个维度的心理症状综合筛查", "scoring": "1-5分深度评分"},
    "9": {"data": SAD_DATA, "desc": "社交回避行为及苦恼感受评估", "scoring": "0-1分双维度评分"},
    "10": {"data": UCLA_DATA, "desc": "主观孤独感程度评估", "scoring": "1-4分评分"},
    "11": {"data": SES_DATA, "desc": "整体自尊水平与自我价值感", "scoring": "1-4分评分"},
    "12": {"data": LES_DATA, "desc": "年度重大生活事件压力总量", "scoring": "多维加权计分"}
}

@register(
    "astrbot_plugin_heart_sunny",
    "chino621",
    "心晴助手 - 心理测评与情绪关怀工具。",
    "v1.0.6"
)
class HeartSunnyPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        try:
            data_dir = StarTools.get_data_dir()
        except Exception:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            data_dir = os.path.join(get_astrbot_data_path(), "plugin_data", "astrbot_plugin_heart_sunny")
            
        self.sessions_file = os.path.join(data_dir, "heart_sunny_sessions.json")
        self.mood_file = os.path.join(data_dir, "heart_sunny_moods.json")
        self.sessions = self._load_json(self.sessions_file)
        self.mood_logs = self._load_json(self.mood_file)
        self.monitor_tasks = {} # 用于超时提醒

    def _load_json(self, path: str) -> Dict[str, Any]:
        if not os.path.exists(path): return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if "sessions" in path:
                now = time.time()
                data = {uid: sess for uid, sess in data.items() if now - sess.get("last_action_time", 0) < 1800}
            return data
        except Exception as e:
            logger.error(f"[心晴助手] 加载失败: {e}")
            return {}

    def _save_json(self, path: str, data: dict):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[心晴助手] 保存失败: {e}")

    def _make_progress_bar(self, score, max_val, length=10):
        filled = int((score / max_val) * length)
        filled = max(0, min(length, filled))
        return "■" * filled + "□" * (length - filled)

    def _options_line(self, data: dict) -> str:
        """从量表 intro 中抽取选项说明，供每题重复展示"""
        intro = data.get("intro", "") or ""
        m = re.search(r"[（(]([^（）()]*=[^（）()]*)[）)]", intro)
        if not m:
            lo, hi = data.get("min_score", 0), data.get("max_score", 1)
            return f"（请回复 {lo}-{hi} 之间的数字）"
        opt = m.group(1)
        for ch in ("，", ",", "、"):
            opt = opt.replace(ch, "  ")
        return "（" + re.sub(r"\s+", " ", opt).strip() + "）"

    def _start_timeout_monitor(self, event: AstrMessageEvent, user_id: str):
        if user_id in self.monitor_tasks:
            self.monitor_tasks[user_id].cancel()
        
        async def monitor():
            try:
                timeout = self.config.get("answer_timeout", 180)
                await asyncio.sleep(timeout / 2)
                if user_id in self.sessions and time.time() - self.sessions[user_id]["last_action_time"] >= timeout / 2:
                    msg = "检测到您长时间未回复。如果不想继续测评，回复“取消”即可终止。"
                    await self.context.send_message(event.unified_msg_origin, MessageChain([Plain(msg)]))
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"[心晴助手] 超时监控异常: {e}")
        
        self.monitor_tasks[user_id] = asyncio.create_task(monitor())

    def _init_session(self, user_id: str, state: str) -> dict:
        """初始化会话状态"""
        session = {
            "state": state,
            "last_action_time": time.time(),
            "last_answer_time": 0
        }
        self.sessions[user_id] = session
        return session

    @filter.command("测评")
    async def start_assessment(self, event: AstrMessageEvent):
        if event.get_group_id():
            yield event.plain_result("本功能仅支持在私聊中使用。")
            return
        user_id = str(event.get_sender_id())
        menu = "【心晴助手 - 测评列表】\n" + "\n".join([f"{k}. {v['data']['name']}\n   └ {v['desc']}" for k, v in SCALE_META.items()])
        menu += "\n\n请回复对应编号开始，或者输入“取消”终止会话。"
        self._init_session(user_id, "selecting")
        self._save_json(self.sessions_file, self.sessions)
        yield event.plain_result(menu)

    @filter.command("打卡")
    async def mood_checkin_start(self, event: AstrMessageEvent):
        if event.get_group_id():
            yield event.plain_result("本功能仅支持在私聊中使用。")
            return
        user_id = str(event.get_sender_id())
        self._init_session(user_id, "mood_checkin")
        self._save_json(self.sessions_file, self.sessions)
        yield event.plain_result("今天的心情如何？回复 1-10 的数字。")

    @filter.command("心情")
    async def view_mood_history(self, event: AstrMessageEvent):
        if event.get_group_id():
            yield event.plain_result("本功能仅支持在私聊中使用。")
            return
        user_id = str(event.get_sender_id())
        logs = self.mood_logs.get(user_id, [])
        if not logs: yield event.plain_result("还没有记录过心情。"); return
        recent = [l for l in logs if l["date"] >= (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")]
        history = "【最近一周心情记录】\n" + "\n".join([f"• {l['date']}: {l['score']}分" for l in recent[-7:]])
        yield event.plain_result(history + DISCLAIMER)

    @filter.command("建议")
    async def get_daily_tip(self, event: AstrMessageEvent):
        if event.get_group_id():
            yield event.plain_result("本功能仅支持在私聊中使用。")
            return
        tips = self.config.get("daily_tips", [
            "今天的你已经很努力了，休息一下吧。",
            "深呼吸，感受当下的空气。",
            "哪怕只是一点点进步，也值得为自己鼓掌。",
            "世界欠你的温柔，我会补给你。",
            "去看看窗外的云吧，它们也很自由。"
        ])
        yield event.plain_result(f"【心晴建议】\n{random.choice(tips)}{DISCLAIMER}")

    @filter.command("作者的话")
    async def get_author_quote(self, event: AstrMessageEvent):
        if event.get_group_id():
            yield event.plain_result("本功能仅支持在私聊中使用。")
            return
        quotes = self.config.get("author_quotes", ["作者还没有写下任何语录哦。"])
        yield event.plain_result(f"【作者的话】\n{random.choice(quotes)}{DISCLAIMER}")

    @filter.command("查看进度")
    async def view_progress(self, event: AstrMessageEvent):
        if event.get_group_id():
            yield event.plain_result("本功能仅支持在私聊中使用。")
            return
        user_id = str(event.get_sender_id())
        if user_id not in self.sessions:
            yield event.plain_result("当前没有进行中的测评。")
            return
        session = self.sessions[user_id]
        if session["state"] == "testing":
            current = session["current_index"]
            total = len(session["items"])
            yield event.plain_result(f"当前测评：{session['name']}\n已完成：{current}/{total} 题\n剩余：{total - current} 题")
        else:
            yield event.plain_result("当前未处于答题状态。")

    @filter.command("急救")
    async def emergency_mode(self, event: AstrMessageEvent):
        if event.get_group_id():
            yield event.plain_result("本功能仅支持在私聊中使用。")
            return
        msg = "【情绪急救】\n1. 4-4-8呼吸法\n2. 5-4-3-2-1感官法\n\n【危机热线】\n• 400-161-9995\n• 12355"
        yield event.plain_result(msg + DISCLAIMER)

    @filter.command("取消")
    async def cancel_session(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        if user_id in self.sessions:
            del self.sessions[user_id]
            self._save_json(self.sessions_file, self.sessions)
            if user_id in self.monitor_tasks:
                self.monitor_tasks[user_id].cancel()
            yield event.plain_result("测评已取消。")
        else:
            yield event.plain_result("当前没有正在进行的测评。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_replies(self, event: AstrMessageEvent):
        if event.get_group_id(): return
        
        text = event.message_str.strip()
        if not text: return # 过滤空消息
        
        commands = ["测评", "打卡", "心情", "建议", "作者的话", "查看进度", "急救", "取消"]
        if text.startswith(("/", "!")) or text in commands or any(text.startswith(cmd) for cmd in commands):
            return
        
        user_id = str(event.get_sender_id())
        if user_id not in self.sessions: return
        
        session = self.sessions[user_id]
        session["last_action_time"] = time.time()

        if session["state"] == "selecting":
            if text in SCALE_META:
                meta = SCALE_META[text]
                session.update({"state": "info", "selected_scale_id": text})
                self._save_json(self.sessions_file, self.sessions)
                yield event.plain_result(f"【量表简介：{meta['data']['name']}】\n• 目的：{meta['desc']}\n• 题量：{len(meta['data']['items'])} 题\n• 说明：{meta['data']['intro']}\n\n确定开始请回复“开始”，或者输入“取消”终止会话。")
            else:
                yield event.plain_result("输入无效，请回复列表中的编号。")
            return

        if session["state"] == "info":
            if text == "开始":
                data = SCALE_META[session["selected_scale_id"]]["data"]
                session.update({
                    "state": "testing", 
                    "name": data["name"], 
                    "items": data["items"], 
                    "min_score": data.get("min_score", 0), 
                    "max_score": data.get("max_score", 0), 
                    "answers": [], 
                    "current_index": 0,
                    "factors": data.get("factors", {}),
                    "last_answer_time": 0
                })
                if data["name"] == "生活事件量表 LES":
                    session["state"] = "les_indices"
                    yield event.plain_result(f"{data['name']}\n请回复发生过的事件编号（如 1,5）：\n" + "\n".join([f"{i+1}. {item}" for i, item in enumerate(data["items"])]))
                else:
                    session["options_line"] = self._options_line(data)
                    yield event.plain_result(f"测评开始！\n\n第 1 题：{data['items'][0]}\n{self._options_line(data)}")
                self._start_timeout_monitor(event, user_id)
            else:
                yield event.plain_result("确定要开始吗？回复“开始”确认，回复“取消”退出。")
            return

        if session["state"] == "mood_checkin":
            try:
                score = int(text)
                if 1 <= score <= 10:
                    date_str = datetime.now().strftime("%Y-%m-%d")
                    if user_id not in self.mood_logs: self.mood_logs[user_id] = []
                    self.mood_logs[user_id] = [l for l in self.mood_logs[user_id] if l["date"] != date_str]
                    self.mood_logs[user_id].append({"date": date_str, "score": score})
                    self._save_json(self.mood_file, self.mood_logs)
                    del self.sessions[user_id]
                    yield event.plain_result(f"记录成功！今天的心情是 {score} 分。")
                else: yield event.plain_result("请输入 1-10 之间的数字。")
            except ValueError: yield event.plain_result("请输入数字 1-10 记录心情。")
            return

        if session["state"] == "les_indices":
            try:
                indices = [int(i.strip()) for i in text.replace("，", ",").split(",") if i.strip().isdigit()]
                if not indices: yield event.plain_result("请输入有效的编号。"); return
                session.update({"les_selected": indices, "current_les_idx": 0, "state": "les_nature"})
                yield event.plain_result(f"事件：{session['items'][indices[0]-1]}\n1. 性质：1=好事，2=坏事")
            except Exception: yield event.plain_result("格式错误，请用逗号分隔编号。")
            return

        if session["state"].startswith("les_"):
            try:
                val = int(text)
                if session["state"] == "les_nature":
                    if val not in [1, 2]: yield event.plain_result("请回复 1 或 2。"); return
                    session.update({"current_les_data": {"nature": val}, "state": "les_impact"})
                    yield event.plain_result("2. 影响程度：0=无，1=轻，2=中，3=重，4=极重")
                elif session["state"] == "les_impact":
                    if not (0 <= val <= 4):
                        yield event.plain_result("请输入 0 到 4 的数字。")
                        return
                    session["current_les_data"]["impact"] = val
                    session["state"] = "les_duration"
                    yield event.plain_result("2. 持续时间：1=半年内，2=一年内，3=二年以上")
                elif session["state"] == "les_duration":
                    if not (1 <= val <= 3):
                        yield event.plain_result("请输入 1 到 3 的数字。")
                        return
                    session["current_les_data"]["duration"] = val
                    session["state"] = "les_frequency"
                    yield event.plain_result("4. 发生次数（数字）：")
                elif session["state"] == "les_frequency":
                    session["answers"].append({"idx": session["les_selected"][session["current_les_idx"]], "data": session["current_les_data"]})
                    session["current_les_idx"] += 1
                    if session["current_les_idx"] < len(session["les_selected"]):
                        session["state"] = "les_nature"
                        yield event.plain_result(f"事件：{session['items'][session['les_selected'][session['current_les_idx']]-1]}\n1. 性质：1=好事，2=坏事")
                    else:
                        async for res in self._finish_assessment(event, user_id): yield res
            except ValueError: yield event.plain_result("请输入数字。")
            return

        if session["state"] == "testing":
            try:
                # 答题时间限制检查
                now = time.time()
                last_time = session.get("last_answer_time", 0)
                if now - last_time < 5:
                    yield event.plain_result("请稍等几秒再作答哦~")
                    return

                score = int(text)
                if not (session["min_score"] <= score <= session["max_score"]):
                    yield event.plain_result(f"请输入 {session['min_score']} 到 {session['max_score']} 的数字。")
                    return
                session["answers"].append(score)
                session["current_index"] += 1
                session["last_answer_time"] = time.time() # 更新答题时间
                self._start_timeout_monitor(event, user_id)

                if session["current_index"] < len(session["items"]):
                    yield event.plain_result(f"第 {session['current_index'] + 1} 题：{session['items'][session['current_index']]}\n{session.get('options_line', '')}")
                else:
                    async for res in self._finish_assessment(event, user_id): yield res
            except ValueError: yield event.plain_result(f"请输入数字 {session['min_score']}-{session['max_score']}。")

    async def _finish_assessment(self, event: AstrMessageEvent, user_id: str):
        session = self.sessions[user_id]
        answers, name = session["answers"], session["name"]
        report_lines = [f"【{name} 测评报告】\n"]
        recommendations = []

        if name == "社交回避及苦恼量表 SAD":
            total = sum(1 if (i+1 in SAD_DATA["yes_score_items"] and v == 1) or (i+1 in SAD_DATA["no_score_items"] and v == 0) else 0 for i, v in enumerate(answers))
            avoid = sum(1 if (i+1 in SAD_DATA["avoidance_items"] and ((i+1 in SAD_DATA["yes_score_items"] and v==1) or (i+1 in SAD_DATA["no_score_items"] and v==0))) else 0 for i, v in enumerate(answers))
            bar = self._make_progress_bar(total, 28)
            report_lines.append(f"总分：{total} 分 {bar}\n• 社交回避：{avoid} | 社交苦恼：{total-avoid}")
            if total > 15: recommendations.append("社交压力较大，建议同步进行 UCLA 孤独感量表测评。")

        elif name == "UCLA 孤独感量表 V3":
            total = sum((5-v if i+1 in UCLA_DATA["reverse_items"] else v) for i, v in enumerate(answers))
            status = "低度" if total <= 33 else "中度" if total <= 44 else "高度"
            bar = self._make_progress_bar(total - 20, 60)
            report_lines.append(f"总分：{total} 分 【{status}孤独】\n{bar}")

        elif name == "自尊量表 SES":
            total = sum((5-v if i+1 in SES_DATA["reverse_items"] else v) for i, v in enumerate(answers))
            status = "较低" if total < 25 else "正常" if total <= 35 else "极高"
            bar = self._make_progress_bar(total - 10, 30)
            report_lines.append(f"总分：{total} 分 【自尊水平：{status}】\n{bar}")

        elif name == "生活事件量表 LES":
            pos = sum(a["data"]["impact"] * a["data"]["duration"] * a["data"]["frequency"] for a in answers if a["data"]["nature"] == 1)
            neg = sum(a["data"]["impact"] * a["data"]["duration"] * a["data"]["frequency"] for a in answers if a["data"]["nature"] == 2)
            report_lines.append(f"• 正性刺激量：{pos}\n• 负性刺激量：{neg}\n• 总刺激量：{pos+neg}")
            if neg > 32: recommendations.append("近期负面生活压力较大，建议进行 SCL-90 综合筛查。")

        elif name in ["SDS 抑郁自评量表", "SAS 焦虑自评量表"]:
            ref = SDS_DATA if "SDS" in name else SAS_DATA
            std = int(sum((5-v if i+1 in ref["reverse_items"] else v) for i,v in enumerate(answers)) * 1.25)
            status = "正常"
            if "SDS" in name: status = "正常" if std < 53 else "轻度" if std <= 62 else "中度" if std <= 72 else "重度"
            else: status = "正常" if std < 50 else "轻度" if std <= 59 else "中度" if std <= 69 else "重度"
            bar = self._make_progress_bar(std - 25, 55)
            report_lines.append(f"标准分：{std} 【{status}倾向】\n{bar}")
            if std > 62: recommendations.append(f"检测到较明显的{'抑郁' if 'SDS' in name else '焦虑'}情绪，建议关注身心健康。")

        elif name == "SCL-90 症状自评量表":
            report_lines.append(f"总均分：{round(sum(answers)/90, 2)}\n因子分析 (均分≥2提示异常)：")
            for f_name, indices in session["factors"].items():
                avg = round(sum(answers[i-1] for i in indices) / len(indices), 2)
                report_lines.append(f"{'⚠️ ' if avg >= 2 else '✅ '}{f_name}：{avg}")
                if avg >= 2:
                    if f_name == "焦虑": recommendations.append("焦虑维度得分较高，建议完成 GAD-7 量表以获取更精准信息。")
                    if f_name == "抑郁": recommendations.append("抑郁维度得分较高，建议完成 PHQ-9 量表。")
        
        elif name == "PHQ-9 抑郁筛查量表":
            total = sum(answers)
            status = "无抑郁" if total <= 4 else "轻度" if total <= 9 else "中度" if total <= 14 else "中重度" if total <= 19 else "重度"
            bar = self._make_progress_bar(total, 27)
            report_lines.append(f"总分：{total} 分 【{status}抑郁】\n{bar}")
            if total >= 10: recommendations.append("抑郁得分较高，建议咨询心理专家。")
            
        elif name == "GAD-7 焦虑筛查量表":
            total = sum(answers)
            status = "无焦虑" if total <= 4 else "轻度" if total <= 9 else "中度" if total <= 14 else "重度"
            bar = self._make_progress_bar(total, 21)
            report_lines.append(f"总分：{total} 分 【{status}焦虑】\n{bar}")
            if total >= 10: recommendations.append("焦虑得分较高，建议咨询心理专家。")

        else: report_lines.append(f"总分：{sum(answers)}")

        if recommendations:
            report_lines.append("\n🌟 【健康建议】")
            report_lines.extend([f"• {r}" for r in recommendations])

        del self.sessions[user_id]
        if user_id in self.monitor_tasks: self.monitor_tasks[user_id].cancel()
        self._save_json(self.sessions_file, self.sessions)
        
        mode = self.config.get("mode", "interpretation")
        if mode == "interpretation":
            interpretation = await self._get_llm_interpretation(name, "\n".join(report_lines))
            final = f"{interpretation}{DISCLAIMER}"
        else:
            final = f"{''.join(report_lines)}{DISCLAIMER}"
        yield event.plain_result(final)

    async def _get_llm_interpretation(self, scale_name: str, score_report: str) -> str:
        provider_id = self.config.get("provider_id", "")
        system_prompt = self.config.get("interpretation_prompt", "你是一位专业的心理助手。")
        
        provider = None
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        
        if not provider:
            provider = self.context.get_using_provider()
            
        if not provider: return score_report
        
        try:
            res = provider.text_chat(prompt=f"量表：{scale_name}\n数据：{score_report}\n请给出专业心理解读：", system_prompt=system_prompt)
            if inspect.iscoroutine(res): return (await res).completion_text.strip()
            full = ""
            async for chunk in res: full += chunk.completion_text if hasattr(chunk, "completion_text") else str(chunk)
            return full.strip()
        except Exception: return score_report
