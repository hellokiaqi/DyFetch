# Third-Party Notices

DyFetch contains code originating from the projects listed below. Copyright remains with the
original authors, and the terms of each license apply to the corresponding portions.

## jiji262/douyin-downloader

- Source: <https://github.com/jiji262/douyin-downloader>
- License: MIT
- Used in: the `dy-downloader/` directory; the API wrappers under `apiproxy/` (`douyinapi`,
  `urls`, `result`, `database`) and the `DouYinCommand.py` entry point, which follow the layout of
  that project's earlier versions; `downloader.py`, rewritten and extended from the original logic.

```
MIT License

Copyright (c) 2026 jiji262

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## X-Bogus implementation by Evil0ctal

- Origin: <https://github.com/Evil0ctal/Douyin_TikTok_Download_API>
- License: Apache License 2.0
- Used in: `dy-downloader/utils/xbogus.py`, which is bundled with the upstream project above. The
  original copyright header is retained in the file.

```
Copyright (C) 2021 Evil0ctal

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

The full Apache License 2.0 text is available in
[licenses/Apache-2.0.txt](licenses/Apache-2.0.txt).

## Notes

DyFetch itself is released under the MIT license; see [LICENSE](LICENSE). The copyright and license
notices above must be retained in redistributions. The Apache-2.0 portion does not require derivative
works to adopt the same license.
