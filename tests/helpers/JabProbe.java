// A small Swing window for the real Java Access Bridge test (tests/test_real_java_bridge.py).
// Run: javac -d build JabProbe.java && java -cp build JabProbe <title>
import java.awt.BorderLayout;
import java.awt.GridLayout;
import javax.swing.JButton;
import javax.swing.JCheckBox;
import javax.swing.JFrame;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JTextField;
import javax.swing.SwingUtilities;

public class JabProbe {
    public static void main(String[] args) {
        final String title = args.length > 0 ? args[0] : "locum JAB probe";
        SwingUtilities.invokeLater(() -> {
            JFrame frame = new JFrame(title);
            frame.setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
            JPanel form = new JPanel(new GridLayout(0, 2, 6, 6));
            JLabel customerLabel = new JLabel("Customer");
            JTextField customer = new JTextField(20);
            customer.getAccessibleContext().setAccessibleName("Customer");
            customerLabel.setLabelFor(customer);
            JCheckBox rush = new JCheckBox("Rush order");
            JLabel status = new JLabel("Status: idle");
            JButton save = new JButton("Save");
            // A label's visible text is its accessible name (JLabel has no AccessibleText unless it is HTML).
            save.addActionListener(e -> {
                status.setText("Saved: " + customer.getText() + (rush.isSelected() ? " (rush)" : ""));
                System.out.println("EVENT save " + status.getText());
                System.out.flush();
            });
            rush.addItemListener(e -> {
                System.out.println("EVENT rush " + rush.isSelected());
                System.out.flush();
            });
            form.add(customerLabel);
            form.add(customer);
            form.add(rush);
            form.add(save);
            frame.getContentPane().add(form, BorderLayout.CENTER);
            frame.getContentPane().add(status, BorderLayout.SOUTH);
            frame.pack();
            frame.setLocation(120, 120);
            frame.setVisible(true);
        });
    }
}
