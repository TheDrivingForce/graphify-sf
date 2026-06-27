public class sf_CallerA {
    public void run() {
        sf_CalleeB b = new sf_CalleeB();
        b.bar(42);
        this.helper();
    }

    public void helper() {
        sf_CalleeB.staticThing();
    }
}
