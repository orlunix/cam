//! Per-tab VT model, stable scroll/selection, keyboard and clipboard adapters.
use base64::{engine::general_purpose::STANDARD, Engine};
use cam_protocol::{error, Result};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier, Style},
    widgets::Widget,
};
use std::{
    io::{Read, Write},
    os::unix::io::AsRawFd,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

pub type Point = (i32, u16);
pub struct Terminal {
    pub parser: vt100::Parser,
    pending: Vec<u8>,
    pub paused: bool,
    pub overflow: bool,
    pub selection: Option<(Point, Point)>,
    pub dragging: bool,
}
impl Terminal {
    pub fn new(rows: u16, cols: u16, history: usize) -> Self {
        Self {
            parser: vt100::Parser::new(rows.max(2), cols.max(2), history),
            pending: vec![],
            paused: false,
            overflow: false,
            selection: None,
            dragging: false,
        }
    }
    pub fn process(&mut self, data: &[u8]) {
        if self.paused {
            if self.pending.len() + data.len() > 8 * 1024 * 1024 {
                self.overflow = true;
            } else if !self.overflow {
                self.pending.extend(data);
            }
        } else {
            self.parser.process(data);
        }
    }
    pub fn scroll(&mut self, delta: i32) {
        self.paused = true;
        let offset = self.parser.screen().scrollback() as i64;
        self.parser
            .set_scrollback((offset + delta as i64).max(0) as usize);
        if self.parser.screen().scrollback() == 0 && self.selection.is_none() {
            self.bottom();
        }
    }
    pub fn bottom(&mut self) {
        self.selection = None;
        self.dragging = false;
        self.parser.set_scrollback(0);
        self.paused = false;
        if !self.overflow {
            self.parser.process(&self.pending);
            self.pending.clear();
        }
    }
    pub fn point(&self, row: u16, col: u16) -> Point {
        (row as i32 - self.parser.screen().scrollback() as i32, col)
    }
    pub fn select(&mut self, row: u16, col: u16, begin: bool) {
        self.paused = true;
        let col = if begin
            && self
                .parser
                .screen()
                .cell(row, col)
                .map(|c| c.is_wide_continuation())
                .unwrap_or(false)
        {
            col.saturating_sub(1)
        } else {
            col
        };
        let p = self.point(row, col);
        if begin || self.selection.is_none() {
            self.selection = Some((p, p));
            self.dragging = true;
        } else if let Some(s) = &mut self.selection {
            s.1 = p;
        }
    }
    pub fn selected(&self, row: u16, col: u16) -> bool {
        if let Some((a, b)) = self.selection {
            let (a, b) = if a <= b { (a, b) } else { (b, a) };
            let p = self.point(row, col);
            p >= a && p <= b
        } else {
            false
        }
    }
    pub fn selection_text(&mut self) -> String {
        let Some((a,b))=self.selection else{return String::new()};
        let (a, b) = if a <= b { (a, b) } else { (b, a) };
        let offset = self.parser.screen().scrollback();
        let (rows, cols) = self.parser.screen().size();
        let mut text = String::new();
        for y in a.0..=b.0 {
            self.parser.set_scrollback((-y).max(0) as usize);
            let row = y.max(0) as u16;
            if row >= rows {
                break;
            }
            let first = if y == a.0 { a.1 } else { 0 };
            let last = if y == b.0 {
                b.1.min(cols.saturating_sub(1))
            } else {
                cols.saturating_sub(1)
            };
            let mut line = String::new();
            for x in first..=last {
                if let Some(cell) = self.parser.screen().cell(row, x) {
                    if cell.is_wide_continuation() {
                        continue;
                    }
                    let value = cell.contents();
                    line.push_str(if value.is_empty() { " " } else { &value });
                }
            }
            text.push_str(line.trim_end());
            if y < b.0 && !self.parser.screen().row_wrapped(row) {
                text.push('\n');
            }
        }
        self.parser.set_scrollback(offset);
        text
    }
    pub fn paste(&self, text: &str) -> Result<Vec<u8>> {
        if text.len() > 65520 {
            return Err(error(
                "paste exceeds the 64 KiB input-event limit; paste a smaller selection",
            ));
        }
        if self.parser.screen().bracketed_paste() {
            Ok(format!("\x1b[200~{text}\x1b[201~").into_bytes())
        } else if text.contains(['\n', '\r']) {
            Err(error(
                "tool has not enabled bracketed paste; multiline paste was withheld",
            ))
        } else {
            Ok(text.as_bytes().to_vec())
        }
    }
}
pub struct View<'a>(pub &'a Terminal);
fn color(value: vt100::Color) -> Color {
    match value {
        vt100::Color::Default => Color::Reset,
        vt100::Color::Idx(i) => Color::Indexed(i),
        vt100::Color::Rgb(r, g, b) => Color::Rgb(r, g, b),
    }
}
impl Widget for View<'_> {
    fn render(self, area: Rect, buf: &mut Buffer) {
        let screen = self.0.parser.screen();
        let (rows, cols) = screen.size();
        for y in 0..area.height.min(rows) {
            for x in 0..area.width.min(cols) {
                if let Some(cell) = screen.cell(y, x) {
                    if cell.is_wide_continuation() {
                        continue;
                    }
                    let mut style = Style::default()
                        .fg(color(cell.fgcolor()))
                        .bg(color(cell.bgcolor()));
                    for (enabled, modifier) in [
                        (cell.bold(), Modifier::BOLD),
                        (cell.italic(), Modifier::ITALIC),
                        (cell.underline(), Modifier::UNDERLINED),
                        (cell.inverse(), Modifier::REVERSED),
                    ] {
                        if enabled {
                            style = style.add_modifier(modifier);
                        }
                    }
                    if self.0.selected(y, x) {
                        // Selection must remain visible when NO_COLOR disables
                        // SGR foreground/background colors in the outer UI.
                        style = Style::default().add_modifier(Modifier::REVERSED);
                    }
                    let text = cell.contents();
                    buf.get_mut(area.x + x, area.y + y)
                        .set_symbol(if text.is_empty() { " " } else { &text })
                        .set_style(style);
                }
            }
        }
    }
}
pub fn encode_key(key: KeyEvent) -> Vec<u8> {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    let alt = key.modifiers.contains(KeyModifiers::ALT);
    let text = match key.code {
        KeyCode::Char(c) if ctrl && c.is_ascii() => vec![(c.to_ascii_lowercase() as u8) & 0x1f],
        KeyCode::Char(c) => c.to_string().into_bytes(),
        KeyCode::Enter => b"\r".to_vec(),
        KeyCode::Backspace => vec![127],
        KeyCode::Esc => vec![27],
        KeyCode::Tab => vec![9],
        KeyCode::BackTab => b"\x1b[Z".to_vec(),
        KeyCode::Up => b"\x1b[A".to_vec(),
        KeyCode::Down => b"\x1b[B".to_vec(),
        KeyCode::Right => b"\x1b[C".to_vec(),
        KeyCode::Left => b"\x1b[D".to_vec(),
        KeyCode::Home => b"\x1b[H".to_vec(),
        KeyCode::End => b"\x1b[F".to_vec(),
        KeyCode::Delete => b"\x1b[3~".to_vec(),
        KeyCode::Insert => b"\x1b[2~".to_vec(),
        KeyCode::F(n) if (1..=4).contains(&n) => {
            format!("\x1bO{}", (b'P' + n - 1) as char).into_bytes()
        }
        _ => vec![],
    };
    if alt && !text.is_empty() {
        [vec![27], text].concat()
    } else {
        text
    }
}
fn clipboard_programs(write: bool) -> Vec<(String, Vec<&'static str>)> {
    let mut programs = vec![];
    if std::env::var_os("WAYLAND_DISPLAY").is_some() {
        programs.push((
            if write { "wl-copy" } else { "wl-paste" },
            if write { vec![] } else { vec!["--no-newline"] },
        ));
    }
    if std::env::var_os("DISPLAY").is_some() {
        programs.push((
            "xclip",
            if write {
                vec!["-selection", "clipboard"]
            } else {
                vec!["-selection", "clipboard", "-o"]
            },
        ));
        programs.push((
            "xsel",
            vec!["--clipboard", if write { "--input" } else { "--output" }],
        ));
    }
    #[cfg(target_os = "macos")]
    programs.push((if write { "pbcopy" } else { "pbpaste" }, vec![]));
    let sibling = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(std::path::Path::to_path_buf));
    let mut resolved = vec![];
    for (program, args) in programs {
        resolved.push((program.to_string(), args.clone()));
        if let Some(dir) = &sibling {
            let path = dir.join(program);
            if path.is_file() {
                resolved.push((path.to_string_lossy().into_owned(), args));
            }
        }
    }
    resolved
}
pub fn copy(text: &str) -> Result<String> {
    if text.is_empty() {
        return Err(error("selection is empty"));
    }
    for (program, args) in clipboard_programs(true) {
        if clipboard_helper(
            &program,
            &args,
            Some(text.as_bytes()),
            Duration::from_secs(2),
        )
        .is_ok()
        {
            return Ok("Copied to local clipboard".into());
        }
    }
    // An unsupported OSC 52 (or broken outer tmux passthrough) can expose
    // its base64 payload as screen text. Never emit one without opt-in.
    if std::env::var("CAM_TUI_CLIPBOARD").as_deref() != Ok("osc52") {
        return Err(error(
            "System clipboard unavailable: install xclip/xsel (X11) or wl-clipboard (Wayland); no terminal request sent",
        ));
    }
    if text.len() > 100_000 {
        return Err(error(
            "selection exceeds OSC 52 limit; use a native clipboard or select less text",
        ));
    }
    let osc = format!("\x1b]52;c;{}\x07", STANDARD.encode(text));
    let stdout = std::io::stdout();
    let mut stdout = stdout.lock();
    if std::env::var_os("TMUX").is_some() {
        write!(
            stdout,
            "\x1bPtmux;{}\x1b\\",
            osc.replace('\x1b', "\x1b\x1b")
        )?;
    } else {
        stdout.write_all(osc.as_bytes())?;
    }
    stdout.flush()?;
    Ok("Copy request sent to your terminal (OSC 52; no clipboard confirmation)".into())
}
pub fn read_clipboard() -> Result<String> {
    for (program, args) in clipboard_programs(false) {
        if let Ok(output) = clipboard_helper(&program, &args, None, Duration::from_secs(2)) {
            return Ok(String::from_utf8(output)?);
        }
    }
    Err(error(
        "local clipboard reader unavailable; use your terminal's Paste shortcut",
    ))
}

fn clipboard_helper(
    program: &str,
    args: &[&str],
    input: Option<&[u8]>,
    timeout: Duration,
) -> Result<Vec<u8>> {
    let mut child = Command::new(program)
        .args(args)
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(if input.is_some() {
            Stdio::null()
        } else {
            Stdio::piped()
        })
        .stderr(Stdio::null())
        .spawn()?;
    let mut write = child.stdin.take();
    let mut read = child.stdout.take();
    let outcome: Result<Vec<u8>> = (|| {
        for fd in write
            .as_ref()
            .map(AsRawFd::as_raw_fd)
            .into_iter()
            .chain(read.as_ref().map(AsRawFd::as_raw_fd))
        {
            let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
            if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0
            {
                return Err(std::io::Error::last_os_error().into());
            }
        }
        let mut written = 0;
        let input = input.unwrap_or_default();
        let mut output = vec![];
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(pipe) = &mut write {
                match pipe.write(&input[written..]) {
                    Ok(n) => written += n,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                    Err(e) => return Err(e.into()),
                }
                if written == input.len() {
                    write.take();
                }
            }
            if let Some(pipe) = &mut read {
                let mut chunk = [0; 8192];
                loop {
                    match pipe.read(&mut chunk) {
                        Ok(0) => {
                            read.take();
                            break;
                        }
                        Ok(n) => {
                            output.extend_from_slice(&chunk[..n]);
                            if output.len() > 1024 * 1024 {
                                return Err(error("clipboard exceeds 1 MiB read limit"));
                            }
                        }
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                        Err(e) => return Err(e.into()),
                    }
                }
            }
            if let Some(status) = child.try_wait()? {
                if !status.success() {
                    return Err(error("clipboard helper failed"));
                }
                if write.is_none() && read.is_none() {
                    return Ok(output);
                }
            }
            if Instant::now() >= deadline {
                return Err(error("clipboard helper timed out"));
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    })();
    if outcome.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }
    outcome
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn clipboard_helpers_are_bounded_and_confirm_exit_status() {
        assert_eq!(
            clipboard_helper(
                "/bin/sh",
                &["-c", "printf clipboard"],
                None,
                Duration::from_secs(1)
            )
            .unwrap(),
            b"clipboard"
        );
        assert!(clipboard_helper("/bin/sleep", &["5"], None, Duration::from_millis(50)).is_err());
        assert!(clipboard_helper(
            "/bin/cat",
            &[],
            Some(&vec![b'x'; 65536]),
            Duration::from_secs(1)
        )
        .is_ok());
    }
    #[test]
    fn utf8_and_escape_chunks_keep_colors() {
        let mut t = Terminal::new(4, 20, 100);
        t.process(b"\x1b[3");
        t.process(b"1m\xe4\xb8");
        t.process(b"\xad\xe6\x96\x87");
        assert!(t.parser.screen().contents().contains("中文"));
        assert_eq!(
            t.parser.screen().cell(0, 0).unwrap().fgcolor(),
            vt100::Color::Idx(1)
        );
    }
    #[test]
    fn selecting_the_second_half_of_wide_text_keeps_whole_characters() {
        let mut t = Terminal::new(3, 20, 100);
        t.process("中文".as_bytes());
        t.select(0, 1, true);
        t.select(0, 3, false);
        assert_eq!(t.selection_text(), "中文");
    }
    #[test]
    fn reaching_bottom_resumes_live_output() {
        let mut t = Terminal::new(3, 10, 100);
        t.process(b"one\r\ntwo\r\nthree\r\nfour");
        t.scroll(1);
        t.process(b"\r\nfive");
        t.scroll(-100);
        assert!(!t.paused);
        assert!(t.parser.screen().contents().contains("five"));
    }
    #[test]
    fn history_and_selection_do_not_move_when_output_arrives() {
        let mut t = Terminal::new(3, 10, 100);
        t.process(b"one\r\ntwo\r\nthree\r\nfour");
        t.scroll(1);
        t.select(0, 0, true);
        t.select(1, 2, false);
        let before = t.selection_text();
        t.process(b"\r\nfive\r\nsix");
        assert_eq!(t.selection_text(), before);
        let mut buffer = Buffer::empty(Rect::new(0, 0, 10, 3));
        View(&t).render(buffer.area, &mut buffer);
        assert!(buffer.get(0, 0).modifier.contains(Modifier::REVERSED));
        assert!(!buffer.get(9, 2).modifier.contains(Modifier::REVERSED));
        t.bottom();
        assert!(t.parser.screen().contents().contains("six"));
    }
    #[test]
    fn paste_never_appends_enter_and_alt_screen_is_preserved() {
        let mut t = Terminal::new(3, 10, 100);
        assert_eq!(t.paste("abc").unwrap(), b"abc");
        assert!(t.paste("a\nb").is_err());
        t.process(b"\x1b[?2004h\x1b[?1049h");
        assert!(t.parser.screen().alternate_screen());
        assert_eq!(t.paste("a\nb").unwrap(), b"\x1b[200~a\nb\x1b[201~");
        t.process(b"\x1b[?1049l");
        assert!(!t.parser.screen().alternate_screen());
    }
}
