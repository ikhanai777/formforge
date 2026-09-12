using System;
using System.Drawing;
using System.Windows.Forms;
using DrawingForge.Core.Logging;

namespace DrawingForge.AddIn.Ui
{
    /// <summary>
    /// Shows what the run is doing and lets the user stop it.
    /// </summary>
    /// <remarks>
    /// The work runs on the thread that opened the dialog, not a worker thread,
    /// and the dialog is kept alive by pumping messages between steps. That is
    /// deliberate: the SOLIDWORKS API is apartment-threaded, and calling it from
    /// a background thread means every call marshals across apartments — which
    /// is slower at best and deadlocks against SOLIDWORKS' own modal dialogs at
    /// worst. Pumping is the ugly-but-correct option here.
    /// </remarks>
    internal sealed class ProgressDialog : Form
    {
        private readonly ProgressBar _bar;
        private readonly Label _status;
        private readonly TextBox _logView;
        private readonly Button _cancel;
        private bool _finished;

        /// <summary>Raised when the user asks to stop.</summary>
        public event EventHandler Cancelled;

        public ProgressDialog(string title)
        {
            Text = title;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterScreen;
            MinimizeBox = false;
            MaximizeBox = false;
            ClientSize = new Size(620, 360);

            _status = new Label
            {
                Left = 12,
                Top = 12,
                Width = 596,
                Height = 18,
                Text = "Starting..."
            };

            _bar = new ProgressBar
            {
                Left = 12,
                Top = 34,
                Width = 596,
                Height = 18,
                Minimum = 0,
                Maximum = 100
            };

            _logView = new TextBox
            {
                Left = 12,
                Top = 62,
                Width = 596,
                Height = 250,
                Multiline = true,
                ReadOnly = true,
                ScrollBars = ScrollBars.Vertical,
                Font = new Font(FontFamily.GenericMonospace, 8.25f)
            };

            _cancel = new Button
            {
                Left = 508,
                Top = 322,
                Width = 100,
                Height = 26,
                Text = "Stop"
            };
            _cancel.Click += OnCancelClicked;

            Controls.Add(_status);
            Controls.Add(_bar);
            Controls.Add(_logView);
            Controls.Add(_cancel);
        }

        /// <summary>
        /// Shows the dialog, runs the work, then leaves the dialog up until the
        /// caller disposes it.
        /// </summary>
        public void Run(Action work)
        {
            if (work == null) return;

            Show();
            Application.DoEvents();

            try
            {
                work();
            }
            finally
            {
                _finished = true;
                _cancel.Text = "Close";
                _status.Text = "Finished.";
                _bar.Value = _bar.Maximum;
                Application.DoEvents();
            }
        }

        /// <summary>Updates the bar and the one-line status.</summary>
        public void Update(int percent, string message)
        {
            _bar.Value = Math.Max(_bar.Minimum, Math.Min(_bar.Maximum, percent));
            if (!string.IsNullOrEmpty(message)) _status.Text = message;
            Application.DoEvents();
        }

        /// <summary>Adds a line to the running log.</summary>
        public void Append(LogLevel level, string message)
        {
            if (string.IsNullOrEmpty(message)) return;
            if (level < LogLevel.Info) return;

            string prefix = level == LogLevel.Error ? "ERROR  "
                          : level == LogLevel.Warning ? "warning "
                          : "";

            _logView.AppendText(prefix + message + Environment.NewLine);
            Application.DoEvents();
        }

        private void OnCancelClicked(object sender, EventArgs e)
        {
            if (_finished)
            {
                Close();
                return;
            }

            _cancel.Enabled = false;
            _status.Text = "Stopping after the current part...";
            EventHandler handler = Cancelled;
            if (handler != null) handler(this, EventArgs.Empty);
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            // Closing the window mid-run would leave the batch with nowhere to
            // report to, so treat it as a stop request instead.
            if (!_finished && e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;
                OnCancelClicked(this, EventArgs.Empty);
                return;
            }
            base.OnFormClosing(e);
        }
    }
}
