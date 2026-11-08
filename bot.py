import os
import sqlite3
import logging
from contextlib import closing
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ['BOT_TOKEN']
ADMIN_ID = int(os.environ['ADMIN_ID'])
CHANNELS = [x.strip() for x in os.environ['REQUIRED_CHANNELS'].split(',') if x.strip()]
if len(CHANNELS) != 2:
    raise ValueError('REQUIRED_CHANNELS must contain exactly two channels')
DB = os.environ.get('DATABASE_PATH', 'videos.db')

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init():
    with closing(db()) as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS series(id INTEGER PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS episodes(id INTEGER PRIMARY KEY, series_id INTEGER NOT NULL, number INTEGER NOT NULL, file_id TEXT NOT NULL, caption TEXT DEFAULT '', UNIQUE(series_id, number));
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, joined_at TEXT DEFAULT CURRENT_TIMESTAMP);''')
        c.execute("INSERT OR IGNORE INTO series(id,title) VALUES(1,'المسلسل')")
        c.commit()

def admin(u): return u and u.id == ADMIN_ID

def subscription_keyboard():
    rows=[]
    for i,ch in enumerate(CHANNELS,1):
        link = 'https://t.me/'+ch[1:] if ch.startswith('@') else (ch if ch.startswith('https://') else None)
        if link: rows.append([B(f'اشترك بالقناة {i}', url=link)])
    rows.append([B('تحقق من الاشتراك ✅', callback_data='verify')])
    return M(rows)

async def subscribed(bot, uid):
    for ch in CHANNELS:
        try:
            m=await bot.get_chat_member(ch, uid)
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED): return False
            if m.status == ChatMemberStatus.RESTRICTED and not m.is_member: return False
        except Exception as e:
            logging.warning('Cannot check membership in %s: %s', ch, e)
            return False
    return True

async def gate(update, context):
    if await subscribed(context.bot, update.effective_user.id): return True
    msg='لازم تشترك بالقناتين أولاً، وبعدها اضغط تحقق من الاشتراك.'
    if update.callback_query:
        await update.callback_query.message.reply_text(msg, reply_markup=subscription_keyboard())
    else:
        await update.effective_message.reply_text(msg, reply_markup=subscription_keyboard())
    return False

async def show_series(msg, page=0):
    page=max(0,page)
    with closing(db()) as c:
        rows=c.execute('SELECT id,title FROM series ORDER BY id DESC LIMIT 8 OFFSET ?', (page*8,)).fetchall()
        count=c.execute('SELECT count(*) FROM series').fetchone()[0]
    buttons=[[B(r['title'][:55],callback_data=f's:{r["id"]}:0')] for r in rows]
    nav=[]
    if page>0: nav.append(B('السابق',callback_data=f'p:{page-1}'))
    if (page+1)*8<count: nav.append(B('التالي',callback_data=f'p:{page+1}'))
    if nav: buttons.append(nav)
    await msg.reply_text('اختر المسلسل أو مجموعة الفيديوهات:' if rows else 'لا توجد فيديوهات منشورة حالياً.',reply_markup=M(buttons) if buttons else None)

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    with closing(db()) as c:
        c.execute('INSERT OR IGNORE INTO users(id) VALUES(?)',(update.effective_user.id,)); c.commit()
    if await gate(update,context): await show_episodes(update.effective_message)

async def panel(update,context):
    if not admin(update.effective_user): return
    await update.effective_message.reply_text('لوحة الإدارة\n/settitle اسم المسلسل — تغيير الاسم\n/upload — بدء رفع الحلقات\n/stop — إيقاف الرفع\n/deleteepisode رقم_الحلقة — حذف حلقة\n/stats — الإحصائيات')

async def settitle(update,context):
    if not admin(update.effective_user): return
    title=' '.join(context.args).strip()
    if not title: return await update.message.reply_text('مثال: /settitle Breaking Bad')
    with closing(db()) as c:
        c.execute('UPDATE series SET title=? WHERE id=1',(title,)); c.commit()
    await update.message.reply_text('تم تحديث اسم المسلسل.')

async def upload(update,context):
    if not admin(update.effective_user): return
    context.user_data['upload_sid']=1
    await update.message.reply_text('أرسل الحلقات بالترتيب، وسيتم ترقيمها تلقائياً. لتحديد رقم حلقة ضع #12 في وصف الفيديو. /stop لإنهاء الرفع.')

async def video(update,context):
    if not admin(update.effective_user): return
    sid=context.user_data.get('upload_sid')
    if not sid: return await update.message.reply_text('اكتب /upload رقم_المسلسل أولاً.')
    media=update.message.video or update.message.document
    if not media: return
    if update.message.document and not (media.mime_type or '').startswith('video/'):
        return await update.message.reply_text('أرسل ملف فيديو فقط.')
    caption=update.message.caption or ''
    with closing(db()) as c:
        if not c.execute('SELECT 1 FROM series WHERE id=?',(sid,)).fetchone():
            context.user_data.pop('upload_sid',None)
            return await update.message.reply_text('المسلسل لم يعد موجوداً.')
        number=None
        if caption.strip().startswith('#'):
            token=caption.strip().split()[0][1:]
            if token.isdigit(): number=int(token)
        if number is None: number=c.execute('SELECT COALESCE(MAX(number),0)+1 FROM episodes WHERE series_id=?',(sid,)).fetchone()[0]
        if number<1: return await update.message.reply_text('رقم الحلقة لازم يكون أكبر من صفر.')
        c.execute('INSERT INTO episodes(series_id,number,file_id,caption) VALUES(?,?,?,?) ON CONFLICT(series_id,number) DO UPDATE SET file_id=excluded.file_id,caption=excluded.caption',(sid,number,media.file_id,caption))
        c.commit()
    await update.message.reply_text(f'تم حفظ الحلقة {number} بنجاح ✅')

async def stop(update,context):
    if not admin(update.effective_user): return
    context.user_data.pop('upload_sid',None)
    await update.message.reply_text('تم إيقاف وضع رفع الحلقات.')

async def deleteepisode(update,context):
    if not admin(update.effective_user): return
    try: num=int(context.args[0])
    except (IndexError,ValueError): return await update.message.reply_text('مثال: /deleteepisode 3')
    with closing(db()) as c:
        cur=c.execute('DELETE FROM episodes WHERE series_id=1 AND number=?',(num,));c.commit()
    await update.message.reply_text('تم حذف الحلقة.' if cur.rowcount else 'الحلقة غير موجودة.')

async def stats(update,context):
    if not admin(update.effective_user): return
    with closing(db()) as c:
        u=c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        s=c.execute('SELECT COUNT(*) FROM series').fetchone()[0]
        e=c.execute('SELECT COUNT(*) FROM episodes').fetchone()[0]
    await update.message.reply_text(f'المستخدمون: {u}\nالمسلسلات: {s}\nالحلقات: {e}')

async def show_episodes(msg,page=0):
    page=max(0,page)
    with closing(db()) as c:
        seriesrow=c.execute('SELECT title FROM series WHERE id=1').fetchone()
        rows=c.execute('SELECT number FROM episodes WHERE series_id=1 ORDER BY number LIMIT 18 OFFSET ?',(page*18,)).fetchall()
        count=c.execute('SELECT COUNT(*) FROM episodes WHERE series_id=1').fetchone()[0]
    buttons=[[B(f'الحلقة {r["number"]}',callback_data=f'e:1:{r["number"]}') for r in rows[i:i+3]] for i in range(0,len(rows),3)]
    nav=[]
    if page>0: nav.append(B('السابق',callback_data=f'ep:{page-1}'))
    if (page+1)*18<count: nav.append(B('التالي',callback_data=f'ep:{page+1}'))
    if nav: buttons.append(nav)
    await msg.reply_text(f'{seriesrow["title"]}\nاختر الحلقة:' if rows else 'لم تتم إضافة حلقات بعد.',reply_markup=M(buttons) if buttons else None)

async def callbacks(update,context):
    q=update.callback_query
    await q.answer()
    data=q.data
    if data=='verify':
        if await gate(update,context): await show_episodes(q.message)
        return
    if not await gate(update,context): return
    if data.startswith('ep:'):
        await show_episodes(q.message,int(data.split(':')[1])); return
    if data.startswith('e:'):
        pass
    if data.startswith('e:'):
        _,sid,num=data.split(':')
        with closing(db()) as c:
            row=c.execute('SELECT file_id,caption FROM episodes WHERE series_id=? AND number=?',(int(sid),int(num))).fetchone()
        if not row: return await q.message.reply_text('الحلقة غير موجودة.')
        try:
            await q.message.reply_video(video=row['file_id'],caption=f'الحلقة {num}',supports_streaming=True)
        except Exception:
            try: await q.message.reply_document(document=row['file_id'],caption=f'الحلقة {num}')
            except Exception:
                logging.exception('Video delivery failed')
                await q.message.reply_text('تعذر إرسال الفيديو. تأكد من أن ملف الحلقة ما زال متاحاً.')

def main():
    init()
    app=Application.builder().token(TOKEN).build()
    for command,func in [('start',start),('menu',start),('admin',panel),('help',panel),('settitle',settitle),('upload',upload),('stop',stop),('deleteepisode',deleteepisode),('deleteseries',deleteseries),('stats',stats)]:
        app.add_handler(CommandHandler(command,func))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.VIDEO | filters.Document.VIDEO),video))
    app.run_polling(drop_pending_updates=True)

if __name__=='__main__': main()
