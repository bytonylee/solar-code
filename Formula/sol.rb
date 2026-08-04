class Sol < Formula
  desc "Python coding-agent harness for Upstage Solar models"
  homepage "https://github.com/bytonylee/solar-code"
  head "https://github.com/bytonylee/solar-code.git", branch: "main"

  depends_on "python"

  def install
    libexec.install "bin", "src", ".env.example"
    (bin / "sol").write <<~SH
      #!/bin/sh
      exec python3 "#{libexec}/bin/sol" "$@"
    SH
    chmod 0755, bin / "sol"
  end

  test do
    system bin / "sol", "--help"
  end
end
