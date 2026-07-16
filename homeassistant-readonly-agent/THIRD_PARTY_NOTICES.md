# Third-party notices

The container image includes third-party software in addition to the original
HA AI System Agent source code.

## signal-cli-rest-api 0.100

- Project: <https://github.com/bbernhard/signal-cli-rest-api>
- Source commit: `a4f5855b65d47bfe427735b5660053d1cc00c580`
- License: MIT
- Local modification: the HTTP listener is bound to `127.0.0.1` instead of all
  container interfaces.

Copyright (c) 2020 bbernhard

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## signal-cli 0.14.5

- Project and corresponding source: <https://github.com/AsamK/signal-cli/tree/v0.14.5>
- License: GPL-3.0

The official `signal-cli-rest-api:0.100` base image also contains OpenJDK,
Ubuntu packages, libsignal components, and their respective license metadata.
Their upstream notices and source references remain applicable.
