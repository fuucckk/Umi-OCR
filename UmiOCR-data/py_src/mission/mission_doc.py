# ===============================================
# =============== 文档 - 任务管理器 ===============
# ===============================================

# API所有页数page 均为1开始

import fitz  # PyMuPDF
import time
import math
from PIL import Image
from io import BytesIO

from umi_log import logger
from .mission import Mission
from .mission_ocr import MissionOCR
from ..ocr.tbpu import getParser
from ..ocr.tbpu import IgnoreArea
from ..ocr.tbpu.parser_tools.paragraph_parse import word_separator  # 上下句间隔符

MinSize = 1080  # 最小渲染分辨率

# 合法文件后缀
DocSuf = [
    ".pdf",
    ".xps",
    ".epub",
    ".mobi",
    ".fb2",
    ".cbz",
]


class FitzOpen:
    def __init__(self, path):
        self._path = path
        self._doc = None

    def __enter__(self):
        self._doc = fitz.open(self._path)
        return self._doc

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._doc.close()


# https://pymupdf.readthedocs.io/en/latest/matrix.html#matrix
# 从变换矩阵中提取角度值，返回0~359整数
def transform_to_rotation(matrix):
    # [1, 0, 0, 1, 0, 0] -> [cos(deg), sin(deg), -sin(deg), cos(deg), 0, 0].
    a, b, c, d, _, _ = matrix
    # 处理缩放和反射
    scale = math.sqrt(a**2 + b**2)
    if scale < 1e-6:
        return 0
    # 归一化以消除缩放影响
    cos_theta = a / scale
    sin_theta = b / scale
    # 检查反射
    determinant = a * d - b * c
    if determinant < 0:
        # 反射情况，调整角度计算
        cos_theta = -cos_theta
    theta_rad = math.atan2(sin_theta, cos_theta)
    theta_deg = math.degrees(theta_rad)
    rounded_deg = round(theta_deg) % 360
    return rounded_deg


class _MissionDocClass(Mission):
    def __init__(self):
        super().__init__()
        self._schedulingMode = "1234"  # 调度方式：顺序
        self._minInterval = 0.1  # msnTask最短调用间隔
        self._lastCallTime = 0  # 上一次调用时间

    # 添加一个文档任务
    # msnInfo: { 回调函数"onXX", 参数"argd":{"tbpu.xx", "ocr.xx"} }
    # msnPath: 单个文档路径
    # pageRange: 页数范围。可选： None 全部页 , [1,3] 页面范围（含开头结束）。
    # pageList: 指定多个页数。可选： [] 使用pageRange设置 , [1,2,3] 指定页数
    # password: 密码（非必填）
    def addMission(self, msnInfo, msnPath, pageRange=None, pageList=[], password=""):
        # =============== 加载文档，获取文档操作对象 ===============
        try:
            doc = fitz.open(msnPath)
        except Exception as e:
            return f"[Error] fitz.open error: {msnPath} {e}"
        if doc.is_encrypted and not doc.authenticate(password):
            if password:
                msg = f"[Error] Incorrect password. 文档已加密，密码错误。 [{password}]"
            else:
                msg = "[Error] Doc encrypted. 文档已加密，请提供密码。"
            return msg
        msnInfo["doc"] = doc
        msnInfo["path"] = msnPath
        # =============== 拦截 onEnd ===============
        msnInfo["sourceOnEnd"] = msnInfo["onEnd"] if "onEnd" in msnInfo else None
        msnInfo["onEnd"] = self._preOnEnd
        # =============== pageRange 页面范围 ===============
        page_count = doc.page_count
        if len(pageList) == 0:
            if isinstance(pageRange, (tuple, list)) and len(pageRange) == 2:
                a, b = pageRange[0], pageRange[1]
                if a < 0:
                    a += page_count + 1
                if b < 0:
                    b += page_count + 1
                if a < 1:
                    return f"[Error] pageRange {pageRange} 范围起始不能小于1"
                if b > page_count:
                    return f"[Error] pageRange {pageRange} 范围结束不能大于页数 {doc.page_count}"
                if a > b:
                    return f"[Error] pageRange {pageRange} 范围错误"
                pageList = list(range(a - 1, b))
            else:
                pageList = list(range(0, page_count))
        # 检查页数列表合法性
        if len(pageList) == 0:
            return "[Error] 页数列表为空"
        for p in pageList:
            if not isinstance(p, int):
                return "[Error] 页数列表内容非整数"
            if not 0 <= p < page_count:
                return f"[Error] 页数列表超出 1~{page_count} 范围"
        msnInfo["pageList"] = pageList
        # =============== tbpu文本块后处理 msnInfo["tbpu"] ===============
        argd = msnInfo["argd"]  # 参数
        msnInfo["tbpu"] = []
        msnInfo["ignoreArea"] = {}
        # 忽略区域
        if "tbpu.ignoreArea" in argd:
            iArea = argd["tbpu.ignoreArea"]
            if isinstance(iArea, list) and len(iArea) > 0:
                msnInfo["ignoreArea"]["obj"] = IgnoreArea(iArea)
                # 范围，负数转为倒数第x页
                igStart = argd.get("tbpu.ignoreRangeStart", 1)
                igEnd = argd.get("tbpu.ignoreRangeEnd", page_count)
                if igStart < 0:
                    igStart += page_count + 1
                if igEnd < 0:
                    igEnd += page_count + 1
                msnInfo["ignoreArea"]["start"] = igStart - 1  # -1是将起始1页转为起始0页
                msnInfo["ignoreArea"]["end"] = igEnd - 1
                logger.debug(f"忽略区域范围： {igStart} ~ {igEnd} 。")
        # 获取排版解析器对象
        if "tbpu.parser" in argd:
            msnInfo["tbpu"].append(getParser(argd["tbpu.parser"]))
        return self.addMissionList(msnInfo, pageList)

    def msnTask(self, msnInfo, pno):  # 执行msn。pno为当前页数
        doc = msnInfo["doc"]  # 文档对象
        page = doc[pno]  # 页面对象
        argd = msnInfo["argd"]  # 参数
        extractionMode = argd["doc.extractionMode"]  # OCR内容模式
        """ mixed - 混合OCR/拷贝文本
            fullPage - 整页强制OCR
            imageOnly - 仅OCR图片
            textOnly - 仅拷贝原有文本 """
        errMsg = ""  # 本次任务流程的异常信息

        # =============== 提取图片和原文本 ===============
        imgs = []  # 待OCR的图片列表
        tbs = []  # text box 文本块列表
        page_rotation = page.rotation  # 获取页面的旋转角度
        if extractionMode == "fullPage":  # 模式：整页强制OCR
            # 检查页面边长，如果低于阈值，则增加放大系数，以提高渲染清晰度
            rect = page.rect
            w, h = abs(rect[2] - rect[0]), abs(rect[3] - rect[1])
            m = min(w, h)
            if m < MinSize:
                zoom = MinSize / max(m, 1)
                matrix = fitz.Matrix(zoom, zoom)
            else:
                zoom = 1
                matrix = fitz.Identity
            p = page.get_pixmap(matrix=matrix)
            bytes = p.tobytes("png")
            scale = 1 / zoom
            imgs.append(
                {"bytes": bytes, "xy": (0, 0), "scale_w": scale, "scale_h": scale}
            )
        else:
            # 获取元素 https://pymupdf.readthedocs.io/en/latest/_images/img-textpage.png
            # https://pymupdf.readthedocs.io/en/latest/textpage.html#structure-of-dictionary-outputs
            # 确保越界图像能被采集 https://github.com/pymupdf/PyMuPDF/issues/3171
            p = page.get_text("dict", clip=fitz.INFINITE_RECT())
            for t in p["blocks"]:  # 遍历区块（段落）
                # ========== 获取图片 ==========
                if t["type"] == 1 and (
                    extractionMode == "imageOnly" or extractionMode == "mixed"
                ):
                    # 提取图片相对旋转角，加上页面旋转角，得到图片绝对旋转角
                    transform = t["transform"]
                    img_rotation = transform_to_rotation(transform)
                    abs_rotation = round(page_rotation+img_rotation) % 360
                    img_bytes = t["image"]  # 图片字节
                    bbox = t["bbox"]  # 图片包围盒
                    # 图片视觉大小、原始大小、缩放比例
                    w1, h1 = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    w2, h2 = t["width"], t["height"]
                    # 特殊情况：图片宽高为0
                    if w2 <= 0 or h2 <= 0:
                        continue
                    # 单独计算宽高的缩放比例
                    scale_w = w1 / w2
                    scale_h = h1 / h2
                    # 如果图片有绝对旋转，则逆向旋转图片字节
                    if page_rotation != 0 or img_rotation != 0:
                        logger.debug(f"P{pno}-{len(imgs)} 旋转：页面{page_rotation}°，图片{img_rotation}°，绝对{abs_rotation}°")
                    if abs_rotation != 0:
                        try:
                            with Image.open(BytesIO(img_bytes)) as pimg:
                                # 记录原图格式
                                format = pimg.format
                                if not format:
                                    format = "PNG"
                                # PDF的旋转是顺时针，需要逆时针旋转图片
                                pimg = pimg.rotate(-abs_rotation, expand=True)
                                # 将旋转后的图片转回bytes
                                buffered = BytesIO()
                                pimg.save(buffered, format=format)
                                img_bytes = buffered.getvalue()
                        except Exception:
                            logger.error(
                                "旋转文档图片异常。", exc_info=True, stack_info=True
                            )
                    # 记录图片
                    imgs.append(
                        {
                            "bytes": img_bytes,
                            "xy": (bbox[0], bbox[1]),
                            "scale_w": scale_w,
                            "scale_h": scale_h,
                        }
                    )
                # ========== 获取文本块 ==========
                elif t["type"] == 0 and (
                    extractionMode == "textOnly" or extractionMode == "mixed"
                ):
                    l = len(t["lines"]) - 1
                    for index, line in enumerate(t["lines"]):  # 遍历每一行
                        # 拼接该行所有子文本块的内容
                        text = ""
                        for span in line["spans"]:
                            text += span["text"]
                        # 提取其他信息，组装为OCR文本块格式
                        if text:
                            # 获取该行的的包围盒
                            b = line["bbox"]
                            if page_rotation == 0:  # 页面没有旋转，直接提取
                                box = [
                                    [b[0], b[1]],
                                    [b[2], b[1]],
                                    [b[2], b[3]],
                                    [b[0], b[3]],
                                ]
                            else:  # 页面有旋转，默认文本行无相对旋转，则反向消除文本的绝对旋转
                                # https://pymupdf.readthedocs.io/en/latest/page.html#Page.derotation_matrix
                                rotation_matrix = page.rotation_matrix
                                b01 = fitz.Point(b[0], b[1]) * rotation_matrix
                                b23 = fitz.Point(b[2], b[3]) * rotation_matrix
                                x0 = min(b01.x, b23.x)
                                x1 = max(b01.x, b23.x)
                                y0 = min(b01.y, b23.y)
                                y1 = max(b01.y, b23.y)
                                box = [
                                    [x0, y0],
                                    [x1, y0],
                                    [x1, y1],
                                    [x0, y1],
                                ]
                            # 组装文本块
                            tb = {
                                "box": box,
                                "text": text,
                                "score": 1,
                                "end": "\n" if index == l else "",  # 结尾符
                                "from": "text",  # 来源：直接提取文本
                            }
                            tbs.append(tb)
        # 补充结尾符
        for i1 in range(len(tbs) - 1):
            if tbs[i1]["end"]:  # 跳过已有结尾符的
                continue
            i2 = i1 + 1
            sep = word_separator(tbs[i1]["text"][-1], tbs[i2]["text"][0])
            tbs[i1]["end"] = sep

        # =============== 调用OCR，将 imgs 的内容提取出来放入 tbs ===============
        if imgs:
            # 提取 "ocr." 开头的参数，组装OCR参数字典
            ocrArgd = {}
            for k in argd:
                if k.startswith("ocr."):
                    ocrArgd[k] = argd[k]
            # 调用OCR，堵塞等待任务完成
            ocrList = MissionOCR.addMissionWait(ocrArgd, imgs)
            # 整理OCR结果
            for o in ocrList:
                res = o["result"]
                if res["code"] == 100:
                    x, y = o["xy"]
                    scale_w = o["scale_w"]
                    scale_h = o["scale_h"]
                    for r in res["data"]:
                        # 将所有文本块的坐标，从图片相对坐标系，转为页面绝对坐标系
                        for bi in range(4):
                            r["box"][bi][0] = r["box"][bi][0] * scale_w + x
                            r["box"][bi][1] = r["box"][bi][1] * scale_h + y
                        r["from"] = "ocr"  # 来源：OCR
                        tbs.append(r)
                elif res["code"] != 101:
                    errMsg += f'[Error] OCR code:{res["code"]} msg:{res["data"]}\n'

        # =============== tbpu文本块后处理 ===============
        # 忽略区域
        if msnInfo["ignoreArea"] and tbs:
            # 检查范围
            igStart = msnInfo["ignoreArea"]["start"]
            igEnd = msnInfo["ignoreArea"]["end"]
            if pno >= igStart and pno <= igEnd:
                tbs = msnInfo["ignoreArea"]["obj"].run(tbs)
        # 其他tbpu
        if msnInfo["tbpu"] and tbs:
            for tbpu in msnInfo["tbpu"]:
                tbs = tbpu.run(tbs)

        # =============== 组装结果字典 resDict ===============
        if errMsg:
            logger.error(f"文档识别异常。P{pno}, errMsg: {errMsg}")
            errMsg = f"[Error] Doc P{pno}\n" + errMsg

        if tbs:  # 有文本
            resDict = {"code": 100, "data": tbs}
        elif errMsg:  # 无文本，有异常
            resDict = {"code": 102, "data": errMsg}
        else:  # 无文本，无异常
            resDict = {"code": 101, "data": ""}

        # ===== 仅提取文本时任务速度过快，频繁回调会导致UI卡死，因此故意引入延迟 =====
        currentTime = time.time()
        elapsedTime = currentTime - self._lastCallTime
        # 如果与上一次调用的时间差小于最短间隔，则睡至满足最短间隔
        if elapsedTime < self._minInterval:
            t = self._minInterval - elapsedTime
            time.sleep(t)
        self._lastCallTime = currentTime
        return resDict

    # 获取一个文档的信息，如页数
    def getDocInfo(self, path):
        try:
            with FitzOpen(path) as doc:
                info = {
                    "path": path,
                    "page_count": doc.page_count,
                    "is_encrypted": doc.is_encrypted,
                }
                return info
        except Exception as e:
            return {"path": path, "error": e}

    # 结束前的处理
    def _preOnEnd(self, msnInfo, msg):
        # 先关闭文档对象，再触发原本的 onEnd ，防止新文档保存到原路径时的冲突
        msnInfo["doc"].close()
        if msnInfo["sourceOnEnd"]:
            msnInfo["sourceOnEnd"](msnInfo, msg)


# 全局 DOC 任务管理器
MissionDOC = _MissionDocClass()
