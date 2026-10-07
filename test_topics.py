"""Проверки тем через настоящий Dispatcher aiogram, без сети и вызовов моделей."""
import asyncio
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ['TG_TOKEN'] = 'test'
os.environ['TG_ALLOWED'] = '111'
os.environ['TG_USERS'] = '444,555'
import bot
import topics
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import ForumTopic, Message, Update, User

GROUP = -1001234567890


class Telegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.next_id = 1000

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b'test'

    async def make_request(self, client, method, timeout=None):
        self.calls.append(method)
        name = method.__api_method__
        if name == 'getMe':
            return User(id=999, is_bot=True, first_name='Bot', username='test_bot')
        if name == 'createForumTopic':
            self.next_id += 1
            return ForumTopic(message_thread_id=self.next_id, name=method.name, icon_color=0)
        if name in ('sendMessage', 'sendDocument', 'editMessageText'):
            self.next_id += 1
            thread = getattr(method, 'message_thread_id', None)
            return Message.model_validate(dict(message_id=self.next_id, date=0,
                chat=dict(id=method.chat_id, type='supergroup' if method.chat_id < 0 else 'private',
                          is_forum=method.chat_id < 0, first_name='User'),
                message_thread_id=thread, is_topic_message=bool(thread),
                from_user=dict(id=999, is_bot=True, first_name='Bot'), text=getattr(method, 'text', None)),
                context={'bot': client})
        return True


class TopicsTest(unittest.IsolatedAsyncioTestCase):
    async def inline_filter(self, function, *args, **kwargs):
        # MagicFilter aiogram синхронный; выполняем его без пула потоков в тестах.
        return function(*args, **kwargs)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        catalog = self.root / 'models.json'
        catalog.write_text(json.dumps({'models': [
            {'slug': 'gpt-6-astra', 'visibility': 'list', 'priority': 0,
             'supported_reasoning_levels': [{'effort': 'high'}]},
            {'slug': 'gpt-sol', 'visibility': 'list', 'priority': 1,
             'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}]}]}))
        self.patches = [patch.object(bot, 'SESSIONS', str(self.root/'sessions')),
                        patch.object(bot, 'HOST_CREDS', str(self.root/'claude-auth')),
                        patch.object(bot, 'CODEX_HOST_AUTH', str(self.root/'codex-auth')),
                        patch.object(bot, 'CODEX_BIN', '/bin/true'),
                        patch.object(bot, 'MODELS_CACHE', str(catalog)),
                        patch.object(bot, 'GROUPS', set()),
                        patch.object(bot, 'ALBUM_WAIT', 0.01),
                        patch.object(bot.asyncio, 'to_thread', self.inline_filter)]
        for p in self.patches:
            p.start()
        bot.locks.clear()
        bot.albums.clear()
        bot.album_deadline.clear()
        self.api = Telegram()
        self.client = Bot('999:TEST', session=self.api)
        self.sequence = 0
        bot.topic_store().enable_group(GROUP)

    async def asyncTearDown(self):
        await self.client.session.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def message(self, thread=10, user=111, text='hello', **fields):
        self.sequence += 1
        data = dict(message_id=self.sequence, date=0, chat=dict(id=GROUP, type='supergroup', is_forum=True),
                    from_user=dict(id=user, is_bot=user == 999, first_name='Tester'), text=text)
        if thread is not None:
            data.update(message_thread_id=thread, is_topic_message=True)
        data.update(fields)
        return Message.model_validate(data, context={'bot': self.client})

    async def feed(self, message):
        await bot.dp.feed_update(self.client, Update(update_id=self.sequence, message=message))

    async def callback(self, data, thread=10, user=111):
        message = self.message(thread=thread, user=999, text='menu')
        await bot.dp.feed_update(self.client, Update.model_validate(dict(update_id=self.sequence,
            callback_query=dict(id=str(self.sequence), from_user=dict(id=user, is_bot=False, first_name='Tester'),
                                chat_instance='test', message=message.model_dump(), data=data))))

    def calls(self, method):
        return [call for call in self.api.calls if call.__api_method__ == method]

    async def create(self, thread=10, owner=111, model='claude:sonnet'):
        await self.feed(self.message(thread, owner, None, forum_topic_created=dict(name=f'Topic {thread}', icon_color=0)))
        if model:
            await self.callback('model:' + model, thread, owner)

    async def test_created_topic_prompts_and_initial_default_is_selected(self):
        await self.create(model=None)
        record = bot.topic_store().get((GROUP,10))
        self.assertEqual(record['owner_id'],111)
        self.assertIsNone(record['engine'])
        prompt = self.calls('sendMessage')[-1]
        self.assertEqual(prompt.message_thread_id,10)
        self.assertIn('Выберите модель',prompt.text)
        await self.callback('model:claude:sonnet')
        self.assertEqual(bot.topic_store().get((GROUP,10))['engine'],'claude')
        self.assertEqual(bot.get_model((GROUP,10)),'sonnet')

    async def test_model_switch_keeps_session_files_and_engine(self):
        await self.create()
        key=(GROUP,10)
        sid=bot.active_session(key)
        cwd=Path(bot.workspace_dir(key))
        (cwd/'files/general/keep.txt').write_text('keep')
        await self.callback('model:claude:opus')
        self.assertEqual(bot.active_session(key),sid)
        self.assertEqual(bot.get_model(key),'opus')
        await self.callback('model:codex:gpt-sol')
        self.assertEqual(bot.get_engine(key),'claude')
        self.assertTrue((cwd/'files/general/keep.txt').exists())
        self.assertIn('закреплён',self.calls('answerCallbackQuery')[-1].text)
        self.assertTrue(all('codex' not in b.callback_data for row in bot.model_kb(key,111).inline_keyboard for b in row))

    async def test_codex_switch_and_model_access(self):
        await self.create(owner=444,model='codex:gpt-sol')
        key=(GROUP,10)
        bot.set_active_session(key,'thread-test','codex')
        await self.callback('model:codex:gpt-6-astra',user=444)
        self.assertEqual(bot.get_model(key),'gpt-sol')
        await self.callback('model:codex:gpt-6-astra',user=111)
        self.assertEqual(bot.get_model(key),'gpt-6-astra')
        self.assertEqual(bot.active_session(key),'thread-test')
        with patch.object(bot,'ask',AsyncMock(return_value='done')) as ask:
            await self.feed(self.message(user=444))
            ask.assert_not_awaited()
        await self.callback('model:claude:opus')
        self.assertEqual(bot.get_engine(key),'codex')

    async def test_only_owner_or_bot_admin_changes_settings(self):
        await self.create(owner=444)
        await self.callback('model:claude:opus',user=555)
        await self.callback('effort:high',user=555)
        self.assertEqual(bot.get_model((GROUP,10)),'sonnet')
        self.assertEqual(bot.get_effort((GROUP,10)),'')
        await self.callback('effort:high',user=444)
        self.assertEqual(bot.get_effort((GROUP,10)),'high')
        await self.callback('model:claude:opus',user=111)
        self.assertEqual(bot.get_model((GROUP,10)),'opus')

    async def test_topics_isolate_files_state_locks_and_outputs(self):
        await self.create(10)
        await self.create(20,model='claude:opus')
        first,second=(GROUP,10),(GROUP,20)
        self.assertNotEqual(bot.active_session(first),bot.active_session(second))
        self.assertNotEqual(bot.chat_dir(first),bot.chat_dir(second))
        self.assertNotEqual(bot.workspace_dir(first),bot.workspace_dir(second))
        self.assertIsNot(bot.locks[first],bot.locks[second])
        async def ask(key,text,images):
            await asyncio.sleep(0)
            (Path(bot.workspace_dir(key))/'files/for_user/result.txt').write_text(str(key))
            return 'done'
        with patch.object(bot,'ask',AsyncMock(side_effect=ask)) as model, patch.object(bot.ChatActionSender,'typing',wraps=bot.ChatActionSender.typing) as activity:
            await self.feed(self.message(thread=20,user=555))
            self.assertEqual(activity.call_args.kwargs['chat_id'],GROUP)
            self.assertEqual(activity.call_args.kwargs['message_thread_id'],20)
            self.assertEqual(model.call_args.args[0],second)
        self.assertEqual(self.calls('sendMessage')[-1].message_thread_id,20)
        self.assertEqual(self.calls('sendDocument')[-1].message_thread_id,20)

    async def test_parallel_topics_and_serial_requests_within_topic(self):
        await self.create(10)
        await self.create(20)
        entered, release=asyncio.Event(),asyncio.Event()
        seen=[]
        async def ask(key,text,images):
            seen.append((key,text))
            if text=='blocked':
                entered.set()
                await release.wait()
            return 'done'
        with patch.object(bot,'ask',AsyncMock(side_effect=ask)):
            blocked=asyncio.create_task(bot.respond(self.message(10,text='blocked'),self.client,'blocked'))
            await entered.wait()
            same=asyncio.create_task(bot.respond(self.message(10,text='later'),self.client,'later'))
            await bot.respond(self.message(20,text='parallel'),self.client,'parallel')
            self.assertEqual([text for _,text in seen],['blocked','parallel'])
            release.set()
            await asyncio.gather(blocked,same)
        self.assertEqual([text for _,text in seen],['blocked','parallel','later'])

    async def test_close_rename_reopen_preserves_session(self):
        await self.create()
        key=(GROUP,10)
        sid=bot.active_session(key)
        await self.feed(self.message(text=None,forum_topic_closed={}))
        self.assertTrue(bot.topic_store().get(key)['closed'])
        await self.feed(self.message(text=None,forum_topic_edited={'name':'Renamed'}))
        self.assertEqual(bot.topic_store().get(key)['name'],'Renamed')
        await self.feed(self.message(text=None,forum_topic_reopened={}))
        self.assertFalse(bot.topic_store().get(key)['closed'])
        self.assertEqual(bot.active_session(key),sid)

    async def test_new_creates_topic_without_resetting_current(self):
        await self.create()
        sid=bot.active_session((GROUP,10))
        await self.feed(self.message(text='/new Another conversation',user=444))
        self.assertEqual(bot.active_session((GROUP,10)),sid)
        created=self.calls('createForumTopic')[-1]
        self.assertEqual(created.name,'Another conversation')
        entries=bot.topic_store().list(GROUP)
        newkey=next((GROUP,t) for t,r in entries if t!=10)
        record=bot.topic_store().get(newkey)
        self.assertEqual(record['owner_id'],444)
        self.assertIsNone(record['engine'])
        self.assertEqual(self.calls('sendMessage')[-2].message_thread_id,newkey[1])
        # Запоздавшее событие от самого бота не подменяет владельца и не дублирует меню.
        count=len(self.calls('sendMessage'))
        await self.feed(self.message(newkey[1],user=999,text=None,forum_topic_created=dict(name=created.name,icon_color=0)))
        self.assertEqual(bot.topic_store().get(newkey)['owner_id'],444)
        self.assertEqual(len(self.calls('sendMessage')),count)

    async def test_missing_create_event_initializes_on_first_message(self):
        with patch.object(bot,'ask',AsyncMock()) as ask:
            await self.feed(self.message(thread=55,text='Question'))
            ask.assert_not_awaited()
        self.assertIn('Выберите модель',self.calls('sendMessage')[-1].text)
        await self.callback('model:claude:sonnet',thread=55)
        self.assertEqual(bot.topic_store().get((GROUP,55))['engine'],'claude')

    async def test_general_topic_is_separate_and_replies_without_thread(self):
        await self.create(10)
        with patch.object(bot,'ask',AsyncMock(return_value='general')):
            await self.feed(self.message(thread=None,text='Question'))
        self.assertIsNone(self.calls('sendMessage')[-1].message_thread_id)
        self.assertEqual(topics.scope(self.message(thread=None)),(GROUP,1))
        self.assertNotEqual(bot.workspace_dir((GROUP,1)),bot.workspace_dir((GROUP,10)))
        self.assertEqual(topics.topic_link(GROUP,1),'https://t.me/c/1234567890/1')

    async def test_group_allows_any_allowed_user_to_activate_and_ignores_unknown_users(self):
        bot.topic_store().write(self.root/'sessions'/str(GROUP)/'forum.json',{'enabled':False})
        await self.feed(self.message(text='/start',user=444))
        self.assertTrue(bot.topic_store().group_enabled(GROUP))
        await self.callback('model:claude:sonnet')
        with patch.object(bot,'ask',AsyncMock()) as ask:
            await self.feed(self.message(user=777))
            await self.feed(self.message(user=999))
            ask.assert_not_awaited()

    async def test_topic_list_links_and_old_switch_callbacks_cannot_change_session(self):
        await self.create(10)
        await self.create(20)
        sid=bot.active_session((GROUP,10))
        await self.feed(self.message(text='/sessions'))
        kb=self.calls('sendMessage')[-1].reply_markup
        self.assertEqual({r[0].url for r in kb.inline_keyboard},
                         {'https://t.me/c/1234567890/10','https://t.me/c/1234567890/20'})
        await self.callback('sess:codex:some-session')
        self.assertEqual(bot.active_session((GROUP,10)),sid)
        self.assertEqual(bot.get_engine((GROUP,10)),'claude')

    async def test_files_before_model_selection_are_kept_for_chosen_engine(self):
        message=self.message(text=None,document=dict(file_id='file',file_unique_id='unique',file_name='input.txt',file_size=4))
        async def download(source,destination):
            Path(destination).write_text('data')
        with patch.object(self.client,'download',side_effect=download),patch.object(bot,'ask',AsyncMock()) as ask:
            await self.feed(message)
            ask.assert_not_awaited()
        cwd=Path(bot.workspace_dir((GROUP,10)))
        self.assertTrue((cwd/'files/from_user/input.txt').exists())
        await self.callback('model:codex:gpt-sol')
        self.assertEqual(bot.workspace_dir((GROUP,10)),str(cwd))
        with patch.object(bot,'ask',AsyncMock(return_value='processed')):
            await self.feed(self.message(text='Process files'))
        self.assertTrue((cwd/'files/general/input.txt').exists())

    async def test_delete_requires_owner_and_does_not_touch_other_topics(self):
        await self.create(10,owner=444)
        await self.create(20)
        key=(GROUP,10)
        sid=bot.active_session(key)
        workspace=Path(bot.workspace_dir(key))
        other=Path(bot.workspace_dir((GROUP,20)))
        (other/'files/general/keep.txt').write_text('keep')
        await self.callback('topicdelete:'+sid,user=555)
        self.assertTrue(workspace.exists())
        await self.callback('topicdelete:'+sid,user=444)
        self.assertFalse(workspace.exists())
        self.assertTrue(bot.topic_store().get(key)['deleted'])
        self.assertTrue((other/'files/general/keep.txt').exists())
        await self.callback('model:claude:opus',user=444)
        self.assertTrue(bot.topic_store().get(key)['deleted'])
        with patch.object(bot,'ask',AsyncMock()) as ask:
            await self.feed(self.message())
            ask.assert_not_awaited()

    async def test_album_ids_are_scoped_to_topic(self):
        await self.create(10)
        await self.create(20)
        async def download(source,destination):
            Path(destination).write_text(source.file_id)
        with patch.object(self.client,'download',side_effect=download),patch.object(bot,'ask',AsyncMock()) as ask:
            for thread in (10,20):
                await self.feed(self.message(thread,text=None,media_group_id='shared',
                    document=dict(file_id=f'file-{thread}',file_unique_id=f'unique-{thread}',file_name='file.txt',file_size=4)))
            await asyncio.sleep(0.1)
            ask.assert_not_awaited()
        for thread in (10,20):
            file=Path(bot.workspace_dir((GROUP,thread)))/'files/from_user/file.txt'
            self.assertEqual(file.read_text(),f'file-{thread}')
        self.assertFalse(bot.albums)

    async def test_settings_click_while_busy_is_answered_without_changing_model(self):
        await self.create()
        async with bot.locks[(GROUP,10)]:
            await self.callback('model:claude:opus')
        self.assertEqual(bot.get_model((GROUP,10)),'sonnet')
        self.assertIn('Дождитесь',self.calls('answerCallbackQuery')[-1].text)

    async def test_anonymous_topic_creation_still_prompts(self):
        message=self.message(user=999,text=None,forum_topic_created=dict(name='Anonymous',icon_color=0))
        message=message.model_copy(update={'from_user':User(id=1087968824,is_bot=True,first_name='GroupAnonymousBot')})
        await self.feed(message)
        self.assertIsNone(bot.topic_store().get((GROUP,10))['engine'])
        self.assertEqual(bot.topic_store().get((GROUP,10))['owner_id'],0)
        self.assertIn('Выберите модель',self.calls('sendMessage')[-1].text)

    async def test_restart_preserves_topic_state(self):
        await self.create(model='codex:gpt-sol')
        key=(GROUP,10)
        bot.set_active_session(key,'persisted-thread','codex')
        reloaded=topics.Store(self.root/'sessions')
        self.assertEqual(reloaded.get(key)['engine'],'codex')
        self.assertEqual(bot.active_session(key),'persisted-thread')
        self.assertEqual(reloaded.list(GROUP)[0][0],10)


if __name__=='__main__':
    unittest.main()
