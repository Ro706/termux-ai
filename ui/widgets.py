from datetime import datetime
from textual.widgets import Static

GLOBE_FRAMES = [
    r"""
       .------.
     .'  .--.  '.
    /  .'    '.  \
   |  /  .--.  \  |
   | |  /    \  | |
   |  \  '--'  /  |
    \  '.    .'  /
     '.  '--'  .'
       '------'
    """,
    r"""
       .------.
     .' .----. '.
    /  /      \  \
   |  |  .--.  |  |
   |  | |    | |  |
   |  |  '--'  |  |
    \  \      /  /
     '. '----' .'
       '------'
    """,
    r"""
       .------.
     .'  .--.  '.
    /  /  __  \  \
   |  |  /  \  |  |
   |  |  \__/  |  |
   |  |   __   |  |
    \  \  \_/ /  /
     '.  '--'  .'
       '------'
    """,
    r"""
       .------.
     .' .----. '.
    /  /      \  \
   |  |  .--.  |  |
   |  | |    | |  |
   |  |  '--'  |  |
    \  \      /  /
     '. '----' .'
       '------'
    """,
]

class GlobeClock(Static):
    """Lightweight animated ASCII globe and device-local clock."""

    def on_mount(self):
        self.frame = 0
        self.set_interval(0.35, self.advance_frame)
        self.set_interval(1.0, self.refresh_clock)
        self.refresh_clock()

    def advance_frame(self):
        self.frame = (self.frame + 1) % len(GLOBE_FRAMES)
        self.refresh_clock()

    def refresh_clock(self):
        now = datetime.now().astimezone()
        globe = GLOBE_FRAMES[self.frame].strip("\\n")
        self.update(
            f"[bold cyan]{globe}[/bold cyan]\\n"
            f"[bold green]{now.strftime('%I:%M:%S %p')}[/bold green]\\n"
            f"[bold white]{now.strftime('%A, %d %B %Y')}[/bold white]\\n"
            f"[dim]{now.strftime('%Z (UTC%z)')}[/dim]"
        )
