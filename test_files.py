"""Очередь файлов: реальные временные каталоги, поддельный Telegram и CLI."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ['TG_TOKEN'] = 'test'
os.environ['TG_ALLOWED'] = '111'
import bot
import session_files


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FilesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(bot, 'SESSIONS', str(self.root / 'sessions')),
                        patch.object(bot, 'HOST_CREDS', str(self.root / 'creds')),
                        patch.object(bot, 'CODEX_HOST_AUTH', str(self.root / 'codex-auth')),
                        patch.object(bot.ChatActionSender, 'typing', return_value=Typing()),
                        patch.object(bot.log, 'exception')]
        for p in self.patches:
            p.start()
        bot.locks.clear()
        self.sent = []
        self.delivered = []
        self.client = SimpleNamespace(download=self.download)
        self.message = self.make_message()
        self.cwd = Path(bot.workspace_dir(111))

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def download(self, source, destination):
        if hasattr(destination, "write"):
            destination.write(source.payload)
        else:
            Path(destination).write_bytes(source.payload)

    async def answer(self, text, **kwargs):
        self.sent.append(text)

    async def answer_document(self, document, **kwargs):
        self.delivered.append((Path(document.path).name, Path(document.path).read_bytes()))

    def make_message(self, name='input.txt', data=b'hello', kind='document', caption='Process this'):
        source = SimpleNamespace(file_name=name, file_size=len(data), payload=data,
                                 mime_type='application/octet-stream', is_video=False,
                                 is_animated=False)
        return SimpleNamespace(chat=SimpleNamespace(id=111), from_user=SimpleNamespace(id=111),
                               caption=caption, media_group_id=None, answer=self.answer,
                               answer_document=self.answer_document, **{kind: source})

    async def test_round_trip(self):
        async def model(chat_id, text, images):
            self.assertEqual((self.cwd / 'files/from_user/input.txt').read_bytes(), b'hello')
            self.assertIn('files/from_user/input.txt', text)
            out = self.cwd / 'files/for_user/reports'
            out.mkdir()
            (out / 'answer.txt').write_bytes(b'answer')
            return 'Done'
        with patch.object(bot, 'ask', model):
            await bot.respond(self.message, self.client, 'question', attachments=[self.message])
        self.assertEqual(self.sent, ['Done'])
        self.assertEqual(self.delivered, [('answer.txt', b'answer')])
        self.assertEqual((self.cwd / 'files/general/input.txt').read_bytes(), b'hello')
        self.assertEqual((self.cwd / 'files/general/reports/answer.txt').read_bytes(), b'answer')
        self.assertEqual(session_files.queued(self.cwd, 'from_user'), [])
        self.assertEqual(session_files.queued(self.cwd, 'for_user'), [])

    async def test_failure_preserves_input_and_partial_output(self):
        async def model(*args):
            (self.cwd / 'files/for_user/unfinished.txt').write_text('partial')
            raise RuntimeError('authentication failed')
        with patch.object(bot, 'ask', model):
            await bot.respond(self.message, self.client, 'process', attachments=[self.message])
        self.assertTrue((self.cwd / 'files/from_user/input.txt').exists())
        self.assertTrue((self.cwd / 'files/for_user/unfinished.txt').exists())
        self.assertEqual(self.delivered, [])
        self.assertIn('authentication failed', self.sent[0])

    async def test_upload_failure_retries_only_unsent_files(self):
        for name in ['a.txt', 'b.txt', 'c.txt']:
            (self.cwd / 'files/for_user' / name).write_text(name)
        async def send(document, **kwargs):
            if Path(document.path).name == 'b.txt':
                raise RuntimeError('network down')
            await self.answer_document(document)
        self.message.answer_document = send
        await bot.send_generated_files(self.message, str(self.cwd))
        self.assertEqual([n for n, _ in self.delivered], ['a.txt', 'c.txt'])
        self.assertEqual([p.name for p in session_files.queued(self.cwd, 'for_user')], ['b.txt'])
        self.message.answer_document = self.answer_document
        await bot.send_generated_files(self.message, str(self.cwd))
        self.assertEqual([n for n, _ in self.delivered], ['a.txt', 'c.txt', 'b.txt'])

    async def test_collisions_and_untrusted_names(self):
        msg = self.make_message('../../input.txt')
        await bot.download_attachment(msg, self.client, self.cwd)
        await bot.download_attachment(msg, self.client, self.cwd)
        (self.cwd / 'files/general/input.txt').write_bytes(b'old')
        session_files.archive(self.cwd, 'from_user')
        files = list((self.cwd / 'files/general').iterdir())
        self.assertEqual(len(files), 3)
        self.assertEqual(sorted(p.read_bytes() for p in files), [b'hello', b'hello', b'old'])
        self.assertEqual(session_files.safe_name('..\\evil.txt'), 'evil.txt')
        self.assertLess(len(session_files.safe_name('я' * 300 + '.txt').encode()), 255)

    async def test_download_failure_does_not_call_model(self):
        async def download(source, destination):
            Path(destination).write_bytes(b'partial')
            raise RuntimeError('connection lost')
        self.client.download = download
        with patch.object(bot, 'ask', new_callable=AsyncMock) as model:
            await bot.respond(self.message, self.client, '', attachments=[self.message])
            model.assert_not_called()
        self.assertEqual(session_files.queued(self.cwd, 'from_user'), [])
        self.assertEqual(list((Path(bot.chat_dir(111)) / 'downloads').iterdir()), [])

    async def test_download_and_upload_limits(self):
        self.message.document.file_size = bot.MAX_DOWNLOAD + 1
        with self.assertRaisesRegex(ValueError, 'Telegram'):
            await bot.download_attachment(self.message, self.client, self.cwd)
        out = self.cwd / 'files/for_user/large.bin'
        out.write_bytes(b'1234')
        with patch.object(bot, 'MAX_UPLOAD', 3):
            await bot.send_generated_files(self.message, str(self.cwd))
        self.assertTrue(out.exists())
        self.assertEqual(self.delivered, [])

    async def test_symlinks_hardlinks_and_fifo_are_not_sent(self):
        outside = self.root / 'secret'
        outside.write_text('secret')
        out = self.cwd / 'files/for_user'
        (out / 'link').symlink_to(outside)
        os.link(outside, out / 'hardlink')
        os.mkfifo(out / 'fifo')
        (out / 'dirlink').symlink_to(self.root, target_is_directory=True)
        await bot.send_generated_files(self.message, str(self.cwd))
        self.assertEqual(self.delivered, [])
        inbox = self.cwd / 'files/from_user'
        inbox.rmdir()
        inbox.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            await bot.download_attachment(self.message, self.client, self.cwd)
        self.assertFalse((self.root / 'input.txt').exists())

    async def test_distinct_sessions_restore_workspace_and_memory(self):
        sid = bot.active_session(111)
        (self.cwd / 'files/general/history.txt').write_text('old')
        bot.set_active_session(111, 'other')
        other = bot.workspace_dir(111)
        self.assertNotEqual(other, str(self.cwd))
        self.assertFalse((Path(other) / 'files/general/history.txt').exists())
        bot.set_active_session(111, sid)
        self.assertEqual(bot.workspace_dir(111), str(self.cwd))
        self.assertIn(str(self.cwd), (self.cwd / 'MEMORY.md').read_text())
        self.assertIn('files/from_user', bot.file_prompt(str(self.cwd), 'hello'))
        self.assertNotEqual(bot.workspace_dir(222), str(self.cwd))

    async def test_new_waits_until_file_response_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def model(*args):
            entered.set()
            await release.wait()
            return 'done'
        with patch.object(bot, 'ask', model):
            response = asyncio.create_task(bot.respond(self.message, self.client, 'process', attachments=[self.message]))
            await entered.wait()
            change = asyncio.create_task(bot.on_new(self.message))
            await asyncio.sleep(0)
            self.assertFalse(change.done())
            self.assertEqual(bot.workspace_dir(111), str(self.cwd))
            release.set()
            await asyncio.gather(response, change)
        self.assertNotEqual(bot.workspace_dir(111), str(self.cwd))
        self.assertTrue((self.cwd / 'files/general/input.txt').exists())

    async def test_codex_new_thread_keeps_workspace_on_error(self):
        bot.set_state(111, 'engine', 'codex')
        cwd = bot.workspace_dir(111)
        (Path(cwd) / 'files/from_user/keep.txt').write_text('keep')
        output = (json.dumps({'type': 'thread.started', 'thread_id': 'thread-1'}) + '\n' +
                  json.dumps({'type': 'turn.failed', 'error': {'message': 'offline'}})).encode()
        process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(output, b'')))
        with patch.object(bot, 'CODEX_BIN', '/bin/codex'), patch.object(bot.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)) as spawn:
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                await bot.ask_codex(111, 'question')
            self.assertEqual(spawn.call_args.kwargs['cwd'], cwd)
            self.assertNotIn('TG_TOKEN', spawn.call_args.kwargs['env'])
            self.assertIn('sandbox_mode="workspace-write"', spawn.call_args.args)
        self.assertEqual(bot.active_session(111), 'thread-1')
        self.assertEqual(bot.workspace_dir(111), cwd)
        bot.set_active_session(111, '', 'codex')
        self.assertNotEqual(bot.workspace_dir(111), cwd)
        bot.set_active_session(111, 'thread-1', 'codex')
        self.assertEqual(bot.workspace_dir(111), cwd)

    async def test_claude_auth_error_is_not_retried_as_new_session(self):
        output = json.dumps({'type': 'result', 'is_error': True, 'result': 'token revoked'}).encode()
        process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(output, b'')))
        with patch.object(bot.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)) as spawn:
            with self.assertRaisesRegex(RuntimeError, 'token revoked'):
                await bot.ask_claude(111, 'question')
            self.assertEqual(spawn.await_count, 1)
            args = spawn.call_args.args
            self.assertIn('--restricted', args)
            self.assertIn('files/for_user', args[args.index('--system-prompt') + 1])
            self.assertEqual(spawn.call_args.kwargs['cwd'], str(self.cwd))

    async def test_claude_missing_session_creates_new(self):
        missing = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b'', b'No conversation found with session ID')))
        good = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b'{"type":"result","result":"OK"}', b'')))
        with patch.object(bot.asyncio, 'create_subprocess_exec', AsyncMock(side_effect=[missing, good])) as spawn:
            self.assertEqual(await bot.ask_claude(111, 'question'), 'OK')
            self.assertIn('--session-id', spawn.call_args.args)

    async def test_all_attachment_kinds_are_saved(self):
        for kind in ('document', 'video', 'animation', 'video_note', 'sticker'):
            msg = self.make_message(kind=kind, name=kind + '.bin')
            path, _ = await bot.download_attachment(msg, self.client, self.cwd)
            self.assertEqual(path.read_bytes(), b'hello')
        photo = self.make_message()
        photo.photo = [SimpleNamespace(file_size=3, payload=b'jpg')]
        path, mime = await bot.download_attachment(photo, self.client, self.cwd)
        self.assertEqual(mime, 'image/jpeg')
        self.assertEqual(path.read_bytes(), b'jpg')

    async def test_mixed_album_is_one_turn_and_all_originals_are_archived(self):
        messages = [self.make_message('photo.jpg', b'jpeg', kind='document'),
                    self.make_message('video.mp4', b'video', kind='video'),
                    self.make_message('report.pdf', b'pdf', kind='document')]
        messages[0].document.mime_type = 'image/jpeg'
        for m in messages:
            m.media_group_id = 'mixed'
        async def model(chat_id, text, images):
            self.assertEqual(len(session_files.queued(self.cwd, 'from_user')), 3)
            self.assertEqual(images, [(b'jpeg', 'image/jpeg')])
            return 'album done'
        with patch.object(bot, 'ALBUM_WAIT', 0.01), patch.object(bot, 'ask', AsyncMock(side_effect=model)) as ask:
            for m in messages:
                await bot.receive_files(m, self.client)
            await asyncio.sleep(0.1)
            self.assertEqual(ask.await_count, 1)
        self.assertEqual(len(list((self.cwd / 'files/general').iterdir())), 3)
        self.assertFalse(bot.albums)

    async def test_failed_or_empty_audio_does_not_save_or_call_model(self):
        pending = self.cwd / 'files/from_user/waiting.txt'
        pending.write_text('waiting')
        for kind in ('voice', 'audio'):
            for transcript in (ValueError('bad audio'), ''):
                message = self.make_message(kind + '.ogg', b'voice', kind=kind, caption=None)
                transcribe = AsyncMock(side_effect=transcript) if isinstance(transcript, Exception) else AsyncMock(return_value=transcript)
                with patch.object(bot.asyncio, 'to_thread', transcribe), patch.object(bot, 'ask', AsyncMock()) as ask:
                    await bot.on_voice(message, self.client)
                    ask.assert_not_awaited()
                self.assertTrue(pending.exists())
                self.assertEqual([p.name for p in session_files.queued(self.cwd, 'from_user')], ['waiting.txt'])
                self.assertEqual(session_files.queued(self.cwd, 'general'), [])
                self.assertFalse(list(self.cwd.rglob('*.ogg')))
        self.assertTrue(any('Не смог расшифровать' in text for text in self.sent))
        self.assertTrue(any('Тишина' in text for text in self.sent))

    async def test_audio_is_not_saved_when_model_fails(self):
        message = self.make_message('voice.ogg', b'voice', kind='voice', caption=None)
        with patch.object(bot.asyncio, 'to_thread', AsyncMock(return_value='Вопрос')), patch.object(bot, 'ask', AsyncMock(side_effect=RuntimeError('offline'))) as ask:
            await bot.on_voice(message, self.client)
            self.assertEqual(ask.call_args.args[1], 'Расшифровка аудио: Вопрос')
        self.assertEqual(session_files.queued(self.cwd, 'from_user'), [])
        self.assertEqual(session_files.queued(self.cwd, 'general'), [])
        self.assertFalse(list(self.cwd.rglob('*.ogg')))

    async def test_audio_caption_still_works_when_transcription_fails(self):
        message = self.make_message('voice.ogg', b'voice', kind='voice', caption='Ответь на этот текст')
        with patch.object(bot.asyncio, 'to_thread', AsyncMock(side_effect=ValueError('bad audio'))), patch.object(bot, 'ask', AsyncMock(return_value='done')) as ask:
            await bot.on_voice(message, self.client)
            self.assertEqual(ask.call_args.args[1], 'Ответь на этот текст')
        self.assertFalse(list(self.cwd.rglob('*.ogg')))
        self.assertFalse((Path(bot.chat_dir(111)) / 'downloads').exists())

    async def test_oversized_audio_is_not_downloaded(self):
        message = self.make_message('large.ogg', kind='voice', caption=None)
        message.voice.file_size = bot.MAX_DOWNLOAD + 1
        with patch.object(self.client, 'download', AsyncMock()) as download, patch.object(bot, 'ask', AsyncMock()) as ask:
            await bot.on_voice(message, self.client)
            download.assert_not_awaited()
            ask.assert_not_awaited()
        self.assertEqual(session_files.queued(self.cwd, 'from_user'), [])

    async def test_files_without_context_wait_for_next_text(self):
        messages = [self.make_message('report.pdf', b'pdf', caption=None),
                    self.make_message('video.mp4', b'video', kind='video', caption='  '),
                    self.make_message(caption=None)]
        messages[-1].photo = [SimpleNamespace(file_size=3, payload=b'jpg')]
        (self.cwd / 'files/for_user/pending.txt').write_text('pending')
        with patch.object(bot, 'ask', AsyncMock(return_value='processed')) as ask:
            for message in messages:
                await bot.receive_files(message, self.client)
            ask.assert_not_awaited()
            bot.ChatActionSender.typing.assert_not_called()
            self.assertEqual(self.sent, [])
            self.assertEqual(self.delivered, [])
            self.assertEqual(len(session_files.queued(self.cwd, 'from_user')), 3)
            self.assertEqual(session_files.queued(self.cwd, 'general'), [])

            async def process(chat_id, text, images):
                self.assertEqual(text, 'Обработай присланные файлы')
                self.assertEqual(len(session_files.queued(self.cwd, 'from_user')), 3)
                return 'processed'
            ask.side_effect = process
            await bot.respond(self.message, self.client, 'Обработай присланные файлы')
            self.assertEqual(ask.await_count, 1)
        self.assertEqual(session_files.queued(self.cwd, 'from_user'), [])
        self.assertEqual(len(session_files.queued(self.cwd, 'general')), 4)
        self.assertEqual(self.delivered, [('pending.txt', b'pending')])

    async def test_album_without_caption_only_saves_files(self):
        messages = [self.make_message('a.pdf', caption=None),
                    self.make_message('b.mp4', kind='video', caption=None)]
        for message in messages:
            message.media_group_id = 'no-caption'
        with patch.object(bot, 'ALBUM_WAIT', 0.01), patch.object(bot, 'ask', AsyncMock()) as ask:
            for message in messages:
                await bot.receive_files(message, self.client)
            await asyncio.sleep(0.1)
            ask.assert_not_awaited()
        self.assertEqual(len(session_files.queued(self.cwd, 'from_user')), 2)
        self.assertEqual(self.sent, [])
        self.assertFalse(bot.albums)

    async def test_uncaptioned_audio_processes_queued_files(self):
        for kind in ('voice', 'audio'):
            document = self.make_message('waiting.txt', caption=None)
            await bot.receive_files(document, self.client)
            message = self.make_message(kind + '.ogg', b'audio', kind=kind, caption=None)
            async def process(chat_id, text, images):
                self.assertEqual(text, 'Расшифровка аудио: Обработай файлы')
                self.assertEqual(len(session_files.queued(self.cwd, 'from_user')), 1)
                return 'done'
            with patch.object(bot.asyncio, 'to_thread', AsyncMock(return_value='Обработай файлы')), patch.object(bot, 'ask', AsyncMock(side_effect=process)) as ask:
                await bot.on_voice(message, self.client)
                self.assertEqual(ask.await_count, 1)
            self.assertEqual(session_files.queued(self.cwd, 'from_user'), [])
            self.assertFalse(list(self.cwd.rglob('*.ogg')))

    async def test_structured_claude_startup_error_is_readable(self):
        output = json.dumps({'type': 'result', 'is_error': True, 'errors': ['socat missing']}).encode()
        process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(output, b'')))
        with patch.object(bot.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)) as spawn:
            with self.assertRaisesRegex(RuntimeError, 'socat missing'):
                await bot.ask_claude(111, 'question')
            self.assertEqual(spawn.await_count, 1)

    async def test_delete_removes_only_selected_workspace(self):
        sid = bot.active_session(111)
        bot.set_active_session(111, 'another')
        other = Path(bot.workspace_dir(111))
        (other / 'files/general/keep.txt').write_text('keep')
        bot.delete_session(111, 'claude', sid)
        self.assertFalse(self.cwd.exists())
        self.assertTrue((other / 'files/general/keep.txt').exists())


if __name__ == '__main__':
    unittest.main()
