import re
from enum import Enum
from typing import (
    Pattern,
    Dict,
    List,
    Tuple,
    Callable,
    Any,
    Coroutine,
    Union, Optional
)
from urllib.parse import quote_plus

import aiohttp
import discord
from discord import Interaction
from discord.ext import commands
from discord.ui import Button, View

token = "YOUR_GITHUB_ACCESS_TOKEN"


GITHUB_RE: Pattern[str] = re.compile(
    r"https://github\.com/(?P<repo>[a-zA-Z\d-]+/[\w.-]+)/blob/"
    r"(?P<path>[^#>]+)(\?[^#>]+)?(#L(?P<start_line>\d+)(([-~:]|(\.\.))L(?P<end_line>\d+))?)"
)

GITHUB_COMMIT_RE = re.compile(
    r"https://github\.com/(?P<repo>[A-z\d-]+/[\w.-]+)/commit/(?P<hash>[A-z\d]{40})"
)

GITHUB_COMMIT_AT = re.compile(
    r"@@ -(?P<sline>\d{1,4}),(?P<spos>\d{1,4}) \+(?P<eline>\d{1,4}),(?P<epos>\d{1,4}) @@"
)

GITHUB_GIST_RE: Pattern[str] = re.compile(
    r"https://gist\.github\.com/([a-zA-Z\d-]+)/(?P<gist_id>[a-zA-Z\d]+)/*"
    r"(?P<revision>[a-zA-Z\d]*)/*#file-(?P<file_path>[^#>]+?)(\?[^#>]+)?"
    r"(-L(?P<start_line>\d+)([-~:]L(?P<end_line>\d+))?)"
)

GITLAB_RE: Pattern[str] = re.compile(
    r"https://gitlab\.com/(?P<repo>[\w.-]+/[\w.-]+)/-/blob/(?P<path>[^#>]+)"
    r"(\?[^#>]+)?(#L(?P<start_line>\d+)(-(?P<end_line>\d+))?)"
)

BITBUCKET_RE: Pattern[str] = re.compile(
    r"https://bitbucket\.org/(?P<repo>[a-zA-Z\d-]+/[\w.-]+)/src/(?P<ref>[\da-zA-Z]+)"
    r"/(?P<file_path>[^#>]+)(\?[^#>]+)?(#lines-(?P<start_line>\d+)(:(?P<end_line>\d+))?)"
)

GITHUB_HEADERS: Dict[str, str] = {
    "Accept": "application/vnd.github.v3.raw",
    "Authorization": "Bearer {token}".format(token=token)
}


def _find_ref(path: str, refs: Tuple) -> Tuple[str, str]:
    ref, fp = path.split("/", 1)

    for _ref in refs:
        if path.startswith(_ref["name"] + "/"):
            ref = _ref["name"]
            fp = path[len(ref) + 1:]
            break
    return ref, fp


def _snippet_to_codeblock(
        file_contents: str,
        fp: str,
        start_line: int,
        end_line: int
) -> str:
    file_contents = file_contents.replace("`", "`\u200b")

    if end_line is None:
        end_line = start_line

    if start_line > end_line:
        start_line, end_line = end_line, start_line

    split_file_contents = file_contents.splitlines()
    if start_line > len(split_file_contents) or end_line < 1:
        return ""

    max_lines = len(split_file_contents)
    start_line, end_line = max(1, start_line), min(len(split_file_contents), end_line)

    contents = "\n".join(
        [
            f">{line} {content}" for line, content in
            enumerate(split_file_contents[start_line - 1:end_line], start_line)
        ]
    )

    before_contents = "\n".join(
        [
            f" {line} {content}" for line, content in enumerate(split_file_contents[
                                                                    (start_line - 5)
                                                                    if (start_line >= 5)
                                                                    else 0:
                                                                    (start_line - 1)
                                                                    if (start_line >= 1)
                                                                    else 0
                                                                ],
                                                                (start_line - 4) if (start_line >= 5) else 1)
        ]
    )
    after_contents = "\n".join(
        [
            f" {line} {content}" for line, content in enumerate(split_file_contents[
                                                                end_line
                                                                if (end_line <= max_lines)
                                                                else max_lines:
                                                                (end_line + 5)
                                                                if (end_line + 5 <= max_lines)
                                                                else max_lines
                                                                ],
                                                                end_line + 1)
        ]
    )
    contents = "\n".join([before_contents, contents, after_contents])
    contents = contents.rstrip().replace("`", "\U0000200B")
    lang = fp.split("/")[-1].split(".")[-1].replace("-", "").replace("+", "").replace("_", "")
    lang = lang if lang.isalnum() else ""

    if start_line == end_line:
        line = f"{fp}\n" \
               f"**line: {start_line}**\n\n"

    else:
        line = f"{fp}\n" \
               f"**lines: {start_line} to {end_line}**\n\n"

    if len(contents) != 0:
        return f"{line}```{lang}\n{contents}\n```"
    return f"{line}```{lang}\n```"


class Status(Enum):
    DEFAULT = "modified"
    MODIFIED = "modified"
    RENAMED = "renamed"
    REMOVED = "removed"
    ADDED = "added"


def _pop(**kwargs) -> Tuple[str, str, str, str]:
    sline = kwargs.get("sline")
    spos = kwargs.get("spos")
    eline = kwargs.get("eline")
    epos = kwargs.get("epos")
    return sline, spos, eline, epos


def _patch(file: Dict[str, Union[str, int]]) -> str:
    filename = file.get("filename", "No name").replace("`", "` \u200b")
    content = file.get("patch", "No contents.").replace("`", "` \u200b")
    content = f"filename: `{filename}`\n\n```diff\n{content}\n```"

    return content


def _renamed(file: Dict[str, Union[str, int]]) -> str:
    filename = file.get("filename", "No name")
    old_filename = file.get("previous_filename", "No old name")

    content = file.get("patch", None)

    content = "file renamed from `{old_filename}` to `{filename}`\n{content}".format(
        old_filename=old_filename,
        filename=filename,
        content=f"```diff\n{content}\n```" if content is not None else "**file no change.**"
    )
    return content


class CustomButton(Button):
    count = 0

    @classmethod
    def _count(cls):
        cls.count += 1

    def __init__(self, file: Dict[str, Union[str, int]], author: Union[discord.User, discord.Member]):
        super().__init__()
        self.file = file
        filename = file.get("filename")
        self.filename = filename
        self.label = filename
        self.custom_id = filename.lower()
        self.style = discord.ButtonStyle.primary
        self.author = author
        self._count()

    async def callback(self, interaction: Interaction):
        if len(self.file.get("patch")) >= 1900:
            await interaction.response.edit_message(content="this file was big changed. so I cannot display diff.")

        else:
            p = "\u200b"
            await interaction.response.edit_message(content=f'filename: `{self.filename}`\n\n```diff\n{self.file.get("patch").replace("`", p)}\n```')


class GitLink(commands.Cog):
    pattern_handlers: List[
        Tuple[
            Pattern[str], Callable[
                [str, ...], Coroutine[Any, Any, str]
            ]
        ]
    ]

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pattern_handlers = [
            (GITHUB_RE, self._fetch_github_snippet),
            (GITHUB_GIST_RE, self._fetch_github_gist_snippet),
            (GITLAB_RE, self._fetch_gitlab_snippet),
            (BITBUCKET_RE, self._fetch_bitbucket_snippet),
            (GITHUB_COMMIT_RE, self._fetch_github_commit_snippet)
        ]

    async def _fetch_github_commit_snippet(
            self,
            repo: str,
            hash: str,
            **kwargs
    ) -> None:
        context: discord.Message = kwargs.get("ctx")

        commits = await self._fetch_response(
            url=f"https://api.github.com/repos/{repo}/commits/{hash}",
            format="json",
            headers=GITHUB_HEADERS
        )
        all_list = []
        button_list = []
        files: List[Dict[str, Union[str, int]]] = commits["files"]
        final_contents = ""
        if len(files) == 1:
            replaced = files[0].get("patch").replace("`", "`\u200b")
            return await context.channel.send(f'```diff\n{replaced}\n```')

        elif len(files) > 25:
            msg = await context.channel.send("too much change at this file. cannot send diff")
            return await msg.delete(delay=2)

        for file in files:
            filename = file.get("filename")
            match file.get("status", None):
                case Status.MODIFIED.value:
                    button_list.append(
                        CustomButton(file=file, author=context.author)
                    )
                    all_list.append(_patch(file=file))

                case Status.ADDED.value:
                    button_list.append(
                        CustomButton(file=file, author=context.author)
                    )
                    all_list.append(_patch(file=file))

                case Status.RENAMED.value:
                    # button_list.append(
                    #     CustomView(file=file)
                    # )
                    all_list.append(_renamed(file=file))

                case Status.REMOVED.value:
                    button_list.append(
                        CustomButton(file=file, author=context.author)
                    )
                    all_list.append(_patch(file=file))

                case None:
                    pass

        if len(button_list) == 1:
            await context.channel.send()
        await context.edit(suppress=True)
        await context.channel.send("**Choose button of filename.**", view=View(*button_list))

    async def _fetch_github_snippet(
            self,
            repo: str,
            path: str,
            start_line: str,
            end_line: str,
            **kwargs
    ) -> str:
        branches = await self._fetch_response(
            url=f"https://api.github.com/repos/{repo}/branches",
            format="json",
            headers=GITHUB_HEADERS
        )

        tags = await self._fetch_response(
            url=f"https://api.github.com/repos/{repo}/tags",
            format="json",
            headers=GITHUB_HEADERS
        )
        refs = branches + tags
        ref, fp = _find_ref(path=path, refs=refs)

        file_contents: str = await self._fetch_response(
            f"https://api.github.com/repos/{repo}/contents/{fp}?ref={ref}",
            "text",
            headers=GITHUB_HEADERS,
        )
        return _snippet_to_codeblock(
            file_contents=file_contents,
            fp=fp,
            start_line=int(start_line),
            end_line=int(end_line) if end_line is not None else int(start_line)
        )

    async def _fetch_github_gist_snippet(
            self,
            gist_id: str,
            revision: str,
            file_path: str,
            start_line: str,
            end_line: str,
            **kwargs
    ):
        gist = await self._fetch_response(
            url=f'https://api.github.com/gists/{gist_id}{f"/{revision}" if len(revision) > 0 else ""}',
            format="json",
            headers=GITHUB_HEADERS,
        )
        gist_file:  Dict[str, str]
        for filename, gist_file in gist["files"].items():
            return _snippet_to_codeblock(
                file_contents=gist_file["content"],
                fp=file_path,
                start_line=int(start_line),
                end_line=int(end_line) if end_line is not None else int(start_line)
            )
        return ""

    async def _fetch_gitlab_snippet(
            self,
            repo: str,
            path: str,
            start_line: str,
            end_line: str,
            **kwargs
    ) -> str:
        """Fetches a snippet from a GitLab repo."""
        enc_repo = quote_plus(repo)

        # Searches the GitLab API for the specified branch
        branches = await self._fetch_response(
            url=f"https://gitlab.com/api/v4/projects/{enc_repo}/repository/branches",
            format="json"
        )
        tags = await self._fetch_response(
            url=f"https://gitlab.com/api/v4/projects/{enc_repo}/repository/tags",
            format="json"
        )
        refs = branches + tags
        ref, file_path = _find_ref(path=path, refs=refs)
        enc_ref = quote_plus(ref)
        enc_file_path = quote_plus(file_path)

        file_contents = await self._fetch_response(
            url=f"https://gitlab.com/api/v4/projects/{enc_repo}/repository/files/{enc_file_path}/raw?ref={enc_ref}",
            format="text",
        )
        return _snippet_to_codeblock(
            file_contents=file_contents,
            fp=file_path,
            start_line=int(start_line),
            end_line=int(end_line) if end_line is not None else int(start_line)
        )

    async def _fetch_bitbucket_snippet(
            self,
            repo: str,
            ref: str,
            file_path: str,
            start_line: str,
            end_line: str,
            **kwargs
    ) -> str:
        """Fetches a snippet from a BitBucket repo."""
        file_contents: str = await self._fetch_response(
            url=f"https://bitbucket.org/{quote_plus(repo)}/raw/{quote_plus(ref)}/{quote_plus(file_path)}",
            format="text",
        )
        return _snippet_to_codeblock(
            file_contents=file_contents,
            fp=file_path,
            start_line=int(start_line),
            end_line=int(end_line) if end_line is not None else int(start_line)
        )

    async def _fetch_response(self, url: str, format: str, **kwargs) -> Union[str, Dict[str, Any]]:
        self.__session = aiohttp.ClientSession(raise_for_status=True, **kwargs)
        async with self.__session.get(url=url) as session:
            if format == "text":
                text = await session.text()
                await self.__session.close()
                return text

            if format == "json":
                json = await session.json()
                await self.__session.close()
                return json

    async def _fetch_snippet(self, ctx: discord.Message, content: str) -> str:
        all_snippets: List[Tuple[int, str]] = []
        for pattern, handler in self.pattern_handlers:
            for match in pattern.finditer(content):
                try:
                    groupdict = match.groupdict()
                    groupdict["ctx"] = ctx
                    snippet: Optional[str] = await handler(**groupdict)

                    if isinstance(snippet, list):
                        all_snippets.extend([(match.start(), spt) for spt in snippet])

                    else:
                        if snippet is not None:
                            all_snippets.append((match.start(), snippet))

                except aiohttp.ClientResponseError as error:
                    raise error

        return list(
            map(lambda x: x[1], sorted(all_snippets))
        )

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        contents: List[str] = await self._fetch_snippet(ctx=message, content=message.content)
        for content in contents:
            if not isinstance(content, str):
                continue
            if 0 <= len(content) < 1990:
                await message.channel.send(content=content)

                try:
                    await message.edit(suppress=True)

                except (discord.NotFound, discord.Forbidden) as error:
                    pass

            else:
                pass


def setup(bot: commands.Bot):
    bot.add_cog(GitLink(bot=bot))
