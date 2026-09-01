"""微信消息规范化解析器：只解析事实，不存储，也不调用模型。"""

import hashlib
import html
import re
import xml.etree.ElementTree as ET

TYPE_NAMES = {1:"文本",3:"图片",34:"语音",42:"名片",43:"视频",47:"表情",
              48:"位置",49:"应用消息",50:"通话",10000:"系统消息",10002:"撤回"}
APP_NAMES = {1:"链接",2:"图片",3:"语音",4:"视频",5:"链接",6:"文件",
             17:"位置",19:"合并转发",33:"小程序",35:"视频号",57:"引用",
             2000:"转账",2001:"红包"}


def _xml_root(text: str):
    """容错解析微信 XML。"""
    if not text or "<" not in text:
        return None
    candidate = text[text.find("<"):].replace("&#x0;","")
    candidate = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", candidate)
    candidate = re.sub(r"&(?!#\d+;|#x[0-9a-fA-F]+;|\w+;)", "&amp;", candidate)
    try:
        return ET.fromstring(candidate)
    except ET.ParseError:
        return None


def _text(root, path: str) -> str:
    """读取 XML 子节点并还原实体。"""
    if root is None:
        return ""
    node = root.find(path)
    return html.unescape((node.text or "").strip()) if node is not None else ""


def _int(value, default=0) -> int:
    """宽容转换微信整数列。"""
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def stable_message_id(raw: dict, source_db: str, source_table: str) -> str:
    """优先使用服务器 ID，否则以数据库来源和本地序列生成稳定 ID。"""
    server_id = _int(raw.get("server_id"))
    if server_id:
        return f"wx:{server_id}"
    identity = "|".join([source_db,source_table,str(raw.get("local_id",0)),
                         str(raw.get("sort_seq",0)),str(raw.get("create_time",0))])
    return "local:"+hashlib.sha256(identity.encode()).hexdigest()[:32]


def _parse_app(text: str) -> tuple[str,str,dict,dict]:
    """分离应用消息的正文、引用证据与附件元数据。"""
    root = _xml_root(text)
    app = root.find(".//appmsg") if root is not None else None
    if app is None and root is not None and root.tag == "appmsg":
        app = root
    title, description = _text(app,"title"), _text(app,"des")
    app_type = _int(_text(app,"type"))
    type_name = APP_NAMES.get(app_type,f"应用消息({app_type})")
    quote, metadata = {}, {"app_type":app_type,"url":_text(app,"url")}
    refer = app.find(".//refermsg") if app is not None else None
    if refer is not None:
        quote = {"sender":_text(refer,"displayname"),"content":_text(refer,"content"),
                 "type":_int(_text(refer,"type")),"server_id":_text(refer,"svrid")}
        type_name = "引用"
    if app_type == 6:
        metadata.update({"filename":title,"extension":_text(app,".//appattach/fileext"),
                         "size":_int(_text(app,".//appattach/totallen")),
                         "md5":_text(app,".//appattach/md5")})
        body = f"[文件] {title}".rstrip()
    elif app_type == 19:
        metadata["record_xml"] = _text(app,".//recorditem")
        body = f"[合并转发] {title}".rstrip()
    else:
        body = title or description or f"[{type_name}]"
    if root is None:
        match = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",text,re.S)
        if match:
            body = html.unescape(match.group(1).strip())
    return type_name,body,quote,metadata


def parse_content(local_type: int, text: str) -> tuple[str,str,dict,dict]:
    """返回类型、展示正文、引用结构和附件元数据。"""
    local_type = _int(local_type) & 0xFFFF
    if local_type == 1:
        return "文本",text,{},{}
    root = _xml_root(text)
    if local_type == 3:
        node = root.find(".//img") if root is not None else None
        meta = {"aes_key":node.get("aeskey",""),"md5":node.get("md5",""),
                "length":_int(node.get("length"))} if node is not None else {}
        return "图片","[图片]",{},meta
    if local_type == 34:
        node = root.find(".//voicemsg") if root is not None else None
        meta = {"duration_ms":_int(node.get("voicelength")),
                "aes_key":node.get("aeskey",""),"format":node.get("voiceformat","silk")
                } if node is not None else {}
        return "语音","[语音待转写]",{},meta
    if local_type == 42:
        name = root.get("nickname","") if root is not None else ""
        return "名片",f"[名片] {name}".rstrip(),{},{"nickname":name}
    if local_type == 43:
        return "视频","[视频]",{},{}
    if local_type == 47:
        return "表情","[表情]",{},{}
    if local_type == 48:
        node = root.find(".//location") if root is not None else None
        meta = {k:node.get(k,"") for k in ("label","poiname","x","y")} if node is not None else {}
        return "位置",f"[位置] {meta.get('poiname') or meta.get('label','')}".rstrip(),{},meta
    if local_type == 49 or "<appmsg" in text:
        return _parse_app(text)
    if local_type == 50:
        return "通话","[通话]",{},{}
    if local_type == 10002:
        return "撤回","[消息已撤回]",{},{}
    if local_type == 10000:
        plain = "".join(root.itertext()).strip() if root is not None else text
        return "系统消息",plain[:500] or "[系统消息]",{},{}
    name = TYPE_NAMES.get(local_type,f"未知类型({local_type})")
    return name,text[:500] or f"[{name}]",{},{}


def normalize_message(raw: dict, contact_wxid: str, contact_name: str,
                      sender_map: dict, source_db: str, source_table: str) -> dict:
    """把数据库行转为统一消息事实对象。sender_map 的值为 wxid。"""
    sender_id = raw.get("sender_id")
    sender_wxid = sender_map.get(sender_id,"")
    local_type = _int(raw.get("local_type")) & 0xFFFF
    if local_type in {10000, 10002}:
        sender_role, sender = "system", "微信系统"
    elif sender_wxid == contact_wxid:
        sender_role, sender = "contact", contact_name
    elif sender_wxid:
        sender_role, sender = "self", "我"
    else:
        sender_role, sender = "unknown", f"未知发送者(sid={sender_id})"
    is_self = sender_role == "self"
    type_name,body,quote,metadata = parse_content(local_type,raw.get("content",""))
    packed = raw.get("packed_info_data")
    if isinstance(packed,bytes):
        # packed_info_data 是 protobuf；先提取其中稳定可辨认的附件 MD5，
        # 未识别字段仍保留在原微信库，不在这里臆测。
        match = re.search(rb"(?i)([0-9a-f]{32})",packed)
        if match and not metadata.get("md5"):
            metadata["file_md5"] = match.group(1).decode("ascii").lower()
    return {"message_id":stable_message_id(raw,source_db,source_table),
      "contact_wxid":contact_wxid,"contact_name":contact_name,
      "create_time":raw.get("create_time",0),"sort_seq":raw.get("sort_seq",0),
      "server_seq":raw.get("server_seq",0),"local_id":raw.get("local_id",0),
      "server_id":raw.get("server_id",0),"sender_id":sender_id,
      "sender_wxid":sender_wxid,"sender":sender,"is_self":is_self,
      "sender_role":sender_role,"sender_resolved":sender_role != "unknown",
      "local_type":local_type,"type":type_name,
      "content":body,"raw_content":raw.get("content",""),"quote":quote,
      "metadata":metadata,"source_db":source_db,"source_table":source_table}
