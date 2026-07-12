"""
BCSFE Web - Battle Cats Save File Editor (網頁版)
Flask backend that wraps the bcsfe Python library.
"""

from flask import Flask, request, jsonify, send_file
import os
import sys
import io
import secrets

# ── 初始化 bcsfe core（必須在 import SaveFile 之前）──────────────────────────
# 複製 __main__.migrate() 的邏輯，但不觸發 CLI
from bcsfe import core, copy_to_data_dir, __version__, __app_name__
from importlib import resources

_data_dir = core.Path.get_data_folder()
_version_path = _data_dir.add("version.txt")
_needs_migrate = (not _version_path.exists()) or (
    _version_path.read().to_str().strip() != __version__
)
if _needs_migrate:
    _files_path = resources.files(__app_name__).joinpath("files")
    copy_to_data_dir(_files_path, _files_path)
    _version_path.write(core.Data(__version__))

core.core_data.init_data()
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)

# In-memory session store  { token: core.SaveFile }
sessions: dict[str, core.SaveFile] = {}


# ── Helper: 從 bytes 載入存檔 ────────────────────────────────────────────────
def load_save_from_bytes(file_bytes: bytes, cc: core.CountryCode | None = None) -> core.SaveFile:
    """依照 SaveManagement.load_save_file_path 的方式載入存檔。"""
    data = core.Data(file_bytes)
    # 嘗試自動偵測地區
    try:
        save_file = core.SaveFile(data, cc)
    except core.CantDetectSaveCCError:
        if cc is None:
            raise
        # 已指定 cc 但還是偵測失敗 → 直接丟出
        raise
    return save_file


# ── Helper: 存檔 → bytes ─────────────────────────────────────────────────────
def save_to_bytes(save_file: core.SaveFile) -> bytes:
    return save_file.to_data().data      # .data 是 bytes


# ── Helper: 取出存檔基本資訊（回傳給前端顯示）────────────────────────────────
def get_save_info(save_file: core.SaveFile) -> dict:
    cc_str = save_file.cc.value if hasattr(save_file.cc, "value") else str(save_file.cc)
    gv_str = str(save_file.game_version) if save_file.game_version else "Unknown"
    iq = save_file.inquiry_code
    iq_masked = (iq[:4] + "***" + iq[-2:]) if len(iq) > 6 else "***"

    info: dict = {
        "cc": cc_str,
        "game_version": gv_str,
        "inquiry_code": iq_masked,
        "catfood": save_file.catfood,
        "xp": save_file.xp,
        "normal_tickets": save_file.normal_tickets,
        "rare_tickets": save_file.rare_tickets,
        "np": save_file.np,
        "leadership": save_file.leadership,
    }

    for field in ("platinum_tickets", "legend_tickets", "platinum_shards"):
        info[field] = getattr(save_file, field, None)

    try:
        cats = save_file.cats.cats
        info["total_cats"] = len(cats)
        info["unlocked_cats"] = sum(1 for c in cats if c.unlocked)
    except Exception:
        info["total_cats"] = 0
        info["unlocked_cats"] = 0

    return info


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "index.html"), encoding="utf-8") as f:
        return f.read()


# ① 上傳 SAVE_DATA 檔案
@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "沒有收到檔案"}), 400

    file_bytes = request.files["file"].read()
    if not file_bytes:
        return jsonify({"error": "檔案是空的"}), 400

    try:
        save_file = load_save_from_bytes(file_bytes)
    except core.CantDetectSaveCCError:
        return jsonify({"error": "無法辨識存檔格式，請確認是正確的 SAVE_DATA 檔案"}), 400
    except core.SaveFileInvalid as e:
        return jsonify({"error": f"存檔無效：{e}"}), 400
    except core.FailedToLoadError as e:
        return jsonify({"error": f"讀取存檔失敗：{e}"}), 400
    except Exception as e:
        return jsonify({"error": f"未知錯誤：{e}"}), 400

    token = secrets.token_hex(16)
    sessions[token] = save_file
    return jsonify({"token": token, "info": get_save_info(save_file)})


# ② 用轉移碼 + 驗證碼從 Ponos 伺服器下載存檔
#    對應原本 server_cli.ServerCLI.download_save()
@app.route("/api/download_from_codes", methods=["POST"])
def download_from_codes():
    data = request.json or {}
    transfer_code    = (data.get("transfer_code") or "").strip()
    confirmation_code = (data.get("confirmation_code") or "").strip()
    cc_str           = (data.get("cc") or "en").strip().lower()

    if not transfer_code or not confirmation_code:
        return jsonify({"error": "請輸入轉移碼和驗證碼"}), 400

    try:
        cc = core.CountryCode(cc_str)
    except Exception:
        return jsonify({"error": f"不支援的地區代碼：{cc_str}"}), 400

    # 使用原始碼相同的版本號（不重要，只為了 client_info 格式正確）
    gv = core.GameVersion(120200)

    try:
        server_handler, result = core.ServerHandler.from_codes(
            transfer_code,
            confirmation_code,
            cc,
            gv,
            print=False,
            save_backup=False,
        )
    except Exception as e:
        return jsonify({"error": f"連線伺服器失敗：{e}"}), 502

    if server_handler is None:
        msg = "轉移碼或驗證碼無效"
        if cc_str == "jp":
            msg += "（台版請選 tw，日版請選 jp）"
        return jsonify({"error": msg}), 400

    save_file = server_handler.save_file
    token = secrets.token_hex(16)
    sessions[token] = save_file
    return jsonify({"token": token, "info": get_save_info(save_file)})


# ③ 修改存檔內容
@app.route("/api/edit", methods=["POST"])
def edit():
    data = request.json or {}
    token = data.get("token")

    if not token or token not in sessions:
        return jsonify({"error": "Session 不存在，請重新上傳存檔"}), 400

    save_file = sessions[token]
    MAX_INT = 2_147_483_647

    def clamp(val, max_v=MAX_INT):
        return max(0, min(int(val), max_v))

    changes: dict = {}

    # 基本道具
    FIELDS = {
        "catfood":          MAX_INT,
        "xp":               MAX_INT,
        "normal_tickets":   MAX_INT,
        "rare_tickets":     MAX_INT,
        "platinum_tickets": MAX_INT,
        "legend_tickets":   MAX_INT,
        "platinum_shards":  MAX_INT,
        "np":               MAX_INT,
        "leadership":       32767,
    }
    for key, max_v in FIELDS.items():
        if data.get(key) is not None:
            try:
                v = clamp(data[key], max_v)
                if hasattr(save_file, key):
                    setattr(save_file, key, v)
                    changes[key] = v
            except (ValueError, TypeError):
                pass

    # 貓咪批次操作
    cat_op = data.get("cat_operation")
    if cat_op:
        try:
            cats = save_file.cats.cats
            if cat_op == "unlock_all":
                for cat in cats:
                    cat.unlock(save_file)
                changes["cat_operation"] = f"解鎖全部 {len(cats)} 隻貓"

            elif cat_op == "max_level_all":
                for cat in cats:
                    if cat.unlocked:
                        max_base = cat.max_upgrade_level.base if cat.max_upgrade_level.base > 0 else 30
                        max_plus = cat.max_upgrade_level.plus if cat.max_upgrade_level.plus > 0 else 0
                        cat.upgrade.base = max_base
                        cat.upgrade.plus = max_plus
                changes["cat_operation"] = "已將全部已解鎖貓升至最高等"

            elif cat_op == "true_form_all":
                count = 0
                for cat in cats:
                    if cat.unlocked and cat.unlocked_forms < 2:
                        cat.unlocked_forms = 2
                        cat.current_form = 2
                        count += 1
                changes["cat_operation"] = f"已將 {count} 隻貓升至真形態"

        except Exception as e:
            changes["cat_operation_error"] = str(e)

    return jsonify({"info": get_save_info(save_file), "changes": changes})


# ④ 上傳存檔至 Ponos 伺服器，取得新轉移碼
#    對應原本 save_management.SaveManagement.save_upload()
@app.route("/api/get_codes", methods=["POST"])
def get_codes():
    data = request.json or {}
    token = data.get("token")

    if not token or token not in sessions:
        return jsonify({"error": "Session 不存在，請重新上傳存檔"}), 400

    save_file = sessions[token]

    try:
        result = core.ServerHandler(save_file, print=False).get_codes(
            upload_managed_items=False
        )
    except Exception as e:
        return jsonify({"error": f"上傳失敗：{e}"}), 502

    if result is None:
        return jsonify({"error": "上傳失敗，請確認網路連線是否正常，或存檔是否有效"}), 502

    transfer_code, confirmation_code = result
    return jsonify({
        "transfer_code": transfer_code,
        "confirmation_code": confirmation_code,
    })


# ⑤ 下載修改後的存檔檔案
@app.route("/api/download", methods=["POST"])
def download():
    data = request.json or {}
    token = data.get("token")

    if not token or token not in sessions:
        return jsonify({"error": "Session 不存在"}), 400

    save_file = sessions[token]

    try:
        result_bytes = save_to_bytes(save_file)
    except Exception as e:
        return jsonify({"error": f"序列化存檔失敗：{e}"}), 500

    return send_file(
        io.BytesIO(result_bytes),
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name="SAVE_DATA",
    )


# ⑥ 清除 session
@app.route("/api/reset", methods=["POST"])
def reset():
    data = request.json or {}
    token = data.get("token")
    if token and token in sessions:
        del sessions[token]
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🐱 BCSFE 網頁版啟動中...")
    print("請在瀏覽器開啟 http://localhost:5000")
    app.run(debug=False, host="0.0.0.0", port=5000)
